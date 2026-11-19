# -*- coding: utf-8 -*-
"""
桌宠生成器 - 可视化打包工具 v2
选择序列帧图片文件夹，配置名称/台词/点击区域(框选+涂色)/尺寸等，一键打包成独立桌宠 exe。

v2 新增：
- 预览图滚轮缩放(鼠标锚点)、中键/右键平移；区域用图片归一化坐标，跟随缩放平移
- 涂色法设定事件区域(不同颜色不同事件)，颜色-事件对照表
- 区域下拉框(点预览区域自动索引)、区域N递增命名、单单元格编辑
- 生成器设置：滚轮调数值开关(默认关)、记住配置、拖拽判定参数
- 修复输入文字前置的 bug（斩断编辑回写链）
"""
import os
import sys
import re
import json
import shutil
import time
import traceback
import subprocess

from PySide6.QtCore import (
    Qt, QThread, Signal, QPoint, QPointF, QRect, QRectF, QEvent, QSettings,
)
from PySide6.QtGui import (
    QImage, QPixmap, QColor, QFont, QPainter, QPen, QBrush, QCursor,
)
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QLineEdit, QTextEdit, QPushButton,
    QFileDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QSpinBox, QComboBox, QProgressBar, QMessageBox, QScrollArea,
    QColorDialog, QSplitter, QToolButton, QCheckBox, QSizePolicy, QFrame,
)


def app_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def template_dir():
    if hasattr(sys, '_MEIPASS'):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


HERE = app_dir()
BSETTINGS = QSettings("DesktopPetStudio", "PetBuilderSettings")
MASK_MAX = 400   # 涂色掩膜长边上限（低分辨率足够命中判定，删除/涂色更快）


def default_out_dir():
    base = HERE
    if os.path.basename(os.path.normpath(base)).lower() == 'dist':
        return base
    return os.path.join(base, 'dist')


DEFAULT_SPEECH = (
    "你好呀，我是你的新伙伴~\n"
    "别一直盯着屏幕啦，休息一下\n"
    "今天也要元气满满哦！\n"
    "（歪头看着你）\n"
    "摸摸头的话，也不是不可以。"
)

REACTION_OPTIONS = [
    ("跟随整体设置", ""),
    ("无动作（只显示文字）", "__none__"),
    ("脸红晃动", "blush"),
    ("开心跳跃", "jump"),
    ("眨眼点头", "wink"),
    ("转身摇摆", "spin"),
]
GLOBAL_REACTIONS = [("脸红晃动", "blush"), ("开心跳跃", "jump"),
                    ("眨眼点头", "wink"), ("转身摇摆", "spin")]

PALETTE = [
    ("#ff6b6b", "红"), ("#ffa94d", "橙"), ("#ffd43b", "黄"),
    ("#69db7c", "绿"), ("#4dabf7", "蓝"), ("#9775fa", "紫"),
    ("#f783ac", "粉"), ("#63e6be", "青"),
]


def natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', s)]


def image_files(folder):
    exts = ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif')
    files = [os.path.join(folder, f) for f in os.listdir(folder) if f.lower().endswith(exts)]
    return sorted(files, key=lambda p: natural_key(os.path.basename(p)))


def safe_name(name):
    return re.sub(r'[\\/:*?"<>|]+', '_', name).strip() or "桌宠"


def free_target_exe(exe_path, exe_name, log):
    if not os.path.exists(exe_path):
        return True
    try:
        os.remove(exe_path)
        log("已清理旧的同名 exe")
        return True
    except Exception:
        pass
    log("目标 exe 正被占用，尝试关闭正在运行的同名桌宠...")
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    try:
        subprocess.run(["taskkill", "/F", "/IM", exe_name],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=flags, timeout=15)
    except Exception:
        pass
    time.sleep(1.5)
    try:
        os.remove(exe_path)
        log("已结束占用进程并清理旧 exe")
        return True
    except Exception as e:
        log("警告：旧 exe 仍无法清理（%s），打包可能失败" % e)
        return False


def python_has_deps(cand):
    if not cand or not os.path.exists(cand):
        return False
    try:
        r = subprocess.run([cand, '-c', 'import PySide6, PyInstaller'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


def find_python():
    if not getattr(sys, 'frozen', False):
        return sys.executable
    candidates = []
    for name in ('python', 'python3', 'py'):
        p = shutil.which(name)
        if p and p not in candidates:
            candidates.append(p)
    for root in (os.environ.get('LOCALAPPDATA', ''), os.environ.get('ProgramFiles', '')):
        if root:
            candidates.append(os.path.join(root, 'Programs', 'Python', 'Python313', 'python.exe'))
    for cand in candidates:
        if python_has_deps(cand):
            return cand
    for cand in candidates:
        if cand and os.path.exists(cand):
            return cand
    return sys.executable


class BuildWorker(QThread):
    log = Signal(str)
    progress = Signal(int)
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, opts):
        super().__init__()
        self.opts = opts

    def emit_log(self, s):
        self.log.emit(str(s))

    def run(self):
        o = self.opts
        try:
            work = os.path.join(HERE, '_build', safe_name(o['name']) + '_' + str(int(time.time())))
            assets = os.path.join(work, 'assets')
            os.makedirs(assets, exist_ok=True)

            files = image_files(o['src_dir'])
            if not files:
                raise RuntimeError("所选文件夹里没有找到图片（支持 png/jpg/webp/bmp/gif）")
            self.emit_log("检测到 %d 张图片，开始处理..." % len(files))
            self.progress.emit(5)

            ratio = None
            for i, src in enumerate(files):
                img = QImage(src)
                if img.isNull():
                    self.emit_log("跳过无法读取的图片: " + os.path.basename(src))
                    continue
                if ratio is None:
                    ratio = img.width() / img.height()
                if o['target_h']:
                    w = max(1, int(round(o['target_h'] * ratio)))
                    scaled = img.scaled(w, o['target_h'], Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
                else:
                    scaled = img
                scaled.save(os.path.join(assets, 'frame_%04d.webp' % i), 'WEBP', o['quality'])
                if (i + 1) % 10 == 0 or i == len(files) - 1:
                    self.emit_log("  已处理 %d/%d" % (i + 1, len(files)))
                    self.progress.emit(5 + int(35 * (i + 1) / len(files)))

            mask_obj = o.get('mask_image')
            if mask_obj is not None and not mask_obj.isNull() and o.get('has_paint'):
                mask_obj.save(os.path.join(assets, 'paint_mask.png'), 'PNG')

            shutil.copy2(os.path.join(template_dir(), 'pet.py'), os.path.join(work, 'pet.py'))
            cfg = {
                "name": o['name'], "frame_count": len(files),
                "default_height": o['height'], "default_fps": o['fps'],
                "speech": [s for s in o['speech'].splitlines() if s.strip()],
                "bubble_text": o['bubble_text'], "bubble_bg": o['bubble_bg'],
                "bubble_border": o['bubble_border'], "bubble_font_size": o['font_size'],
                "click_reactions": o['click_reactions'],
                "double_click_hide": o['double_click_hide'], "edge_hide": o['edge_hide'],
                "regions": o['regions'], "hit_mode": o['hit_mode'],
                "region_drag_block": o['region_drag_block'], "drag_threshold": o['drag_threshold'],
            }
            with open(os.path.join(work, 'config.json'), 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)

            self.emit_log("资源处理完成，开始打包 exe（约需 30-90 秒）...")
            self.progress.emit(42)

            os.makedirs(o['out_dir'], exist_ok=True)
            target_exe = os.path.join(o['out_dir'], safe_name(o['name']) + '.exe')
            free_target_exe(target_exe, safe_name(o['name']) + '.exe', self.emit_log)
            sep = os.pathsep
            cmd = [
                find_python(), '-m', 'PyInstaller', '--noconfirm', '--onefile', '--windowed', '--clean',
                '--name', safe_name(o['name']),
                '--add-data', 'assets' + sep + 'assets',
                '--add-data', 'config.json' + sep + '.',
                '--distpath', o['out_dir'],
                '--workpath', os.path.join(work, 'work'),
                '--specpath', work,
                'pet.py',
            ]
            self.emit_log("打包命令已启动")
            env = os.environ.copy()
            env['PYTHONIOENCODING'] = 'utf-8'; env['PYTHONUTF8'] = '1'
            proc = subprocess.Popen(
                cmd, cwd=work, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', errors='replace',
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), env=env,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    self.emit_log(line)
            if proc.wait() != 0:
                raise RuntimeError("PyInstaller 打包失败")

            exe = os.path.join(o['out_dir'], safe_name(o['name']) + '.exe')
            if not os.path.exists(exe):
                raise RuntimeError("未找到生成的 exe 文件")
            self.progress.emit(100)
            self.emit_log("完成！输出: " + exe)
            self.done.emit(exe)
        except Exception:
            self.failed.emit(traceback.format_exc())


class RegionPreview(QFrame):
    """
    首帧预览 + 区域编辑：
    - 框选(rect)：左键拖拽画矩形
    - 涂色(paint)：选颜色后左键涂抹，命中 mask 中对应颜色才算
    - 滚轮以鼠标为锚点缩放；中键/右键拖动平移
    - 区域一律用【图片归一化坐标 0~1】存储，缩放/平移只改显示映射 → 天然跟随
    """
    zoneClicked = Signal(int)
    zonesChanged = Signal()

    def __init__(self):
        super().__init__()
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet("background:#20232b;border:1px solid #3a3f4b;border-radius:8px;")
        self.setMinimumSize(260, 420)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)
        self.setContextMenuPolicy(Qt.PreventContextMenu)
        self._pm = QPixmap()
        self._mask = QImage()
        self.zones = []
        self.selected = -1
        self.mode = 'rect'
        self.brush_color = PALETTE[0][0]
        self.brush_size = 18
        self.zoom = 1.0
        self._pan = QPointF(0, 0)
        self._drag_start = None
        self._drag_cur = None
        self._panning = False
        self._pan_start = None
        self._pan_origin = None

    def set_image(self, path):
        self._pm = QPixmap(path)
        if not self._pm.isNull():
            pw, ph = self._pm.width(), self._pm.height()
            if ph >= pw:
                mh = MASK_MAX; mw = max(1, int(MASK_MAX * pw / ph))
            else:
                mw = MASK_MAX; mh = max(1, int(MASK_MAX * ph / pw))
            self._mask = QImage(mw, mh, QImage.Format_ARGB32)
            self._mask.fill(QColor(0, 0, 0, 0))
        self.reset_view()

    def clear_zones(self):
        self.zones = []
        self.selected = -1
        if not self._mask.isNull():
            self._mask.fill(QColor(0, 0, 0, 0))
        self.zonesChanged.emit()
        self.update()

    def mask_image(self):
        return self._mask

    def has_paint(self):
        return any(z.get('type') == 'paint' for z in self.zones)

    def reset_view(self):
        self.zoom = 1.0
        self._pan = QPointF(0, 0)
        self.update()

    # ---- 视图几何 ----
    def _view_geom(self):
        pw, ph = self._pm.width(), self._pm.height()
        if pw <= 0 or ph <= 0 or self.width() <= 2 or self.height() <= 2:
            return None
        scale = min(self.width() / pw, self.height() / ph) * self.zoom
        dw, dh = pw * scale, ph * scale
        ox = (self.width() - dw) / 2 + self._pan.x()
        oy = (self.height() - dh) / 2 + self._pan.y()
        return (ox, oy, dw, dh)

    def _to_norm(self, pt):
        g = self._view_geom()
        if not g:
            return None
        ox, oy, dw, dh = g
        return (max(0.0, min(1.0, (pt.x() - ox) / dw)),
                max(0.0, min(1.0, (pt.y() - oy) / dh)))

    def _to_widget(self, nx, ny):
        g = self._view_geom()
        if not g:
            return QPoint(0, 0)
        ox, oy, dw, dh = g
        return QPoint(int(ox + nx * dw), int(oy + ny * dh))

    def _to_pixel(self, pt):
        n = self._to_norm(pt)
        if not n or self._mask.isNull():
            return None
        return (int(n[0] * max(0, self._mask.width() - 1)),
                int(n[1] * max(0, self._mask.height() - 1)))

    def _brush_mask_px(self):
        if self._pm.isNull() or self._mask.isNull():
            return self.brush_size
        return max(2, int(self.brush_size * self._mask.width() / max(1, self._pm.width())))

    # ---- 绘制 ----
    def paintEvent(self, _):
        super().paintEvent(_)
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        g = self._view_geom()
        if g and not self._pm.isNull():
            ox, oy, dw, dh = g
            target = QRect(int(ox), int(oy), int(dw), int(dh))
            p.drawPixmap(target, self._pm)
            if not self._mask.isNull():
                p.setOpacity(0.72)
                p.drawImage(target, self._mask)
                p.setOpacity(1.0)
        for i, z in enumerate(self.zones):
            x0, y0, x1, y1 = z['rect']
            a = self._to_widget(min(x0, x1), min(y0, y1))
            b = self._to_widget(max(x0, x1), max(y0, y1))
            rect = QRect(a, b).normalized()
            sel = (i == self.selected)
            is_paint = z.get('type') == 'paint' and z.get('color')
            color = QColor(z.get('color', '')) if is_paint else QColor('#7aa2e3')
            p.fillRect(rect, QColor(color.red(), color.green(), color.blue(), 58 if sel else 30))
            pen = QPen(QColor('#ff8a8a') if sel else color)
            pen.setWidth(2 if sel else 1)
            p.setPen(pen); p.setBrush(QBrush(Qt.NoBrush))
            p.drawRect(rect)
            p.setPen(QColor('#ffffff') if sel else QColor('#cfe0f7'))
            p.setFont(QFont('Microsoft YaHei', 10))
            label = ("◉ " if is_paint else "") + "%d %s" % (i + 1, z.get('name', '区域'))
            p.drawText(rect.topLeft() + QPoint(5, 15), label)
        if self.mode == 'rect' and self._drag_start and self._drag_cur:
            rect = QRect(self._drag_start, self._drag_cur).normalized()
            pen = QPen(QColor('#ffd166')); pen.setWidth(2); pen.setStyle(Qt.DashLine)
            p.setPen(pen); p.setBrush(QBrush(Qt.NoBrush)); p.drawRect(rect)
        p.end()

    # ---- 涂色 ----
    def _paint_at(self, pt):
        px = self._to_pixel(pt)
        if not px or self._mask.isNull():
            return
        painter = QPainter(self._mask)
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(self.brush_color), self._brush_mask_px() * 2,
                   Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        painter.setPen(pen)
        if self._drag_start is not None and self._drag_cur is not None:
            a = self._to_pixel(self._drag_start)
            b = self._to_pixel(self._drag_cur)
            if a and b:
                painter.drawLine(a[0], a[1], b[0], b[1])
        else:
            painter.drawPoint(px[0], px[1])
        painter.end()
        self.update()

    def _erase_color(self, color_hex):
        if self._mask.isNull():
            return
        want = QColor(color_hex)
        w, h = self._mask.width(), self._mask.height()
        wr, wg, wb = want.red(), want.green(), want.blue()
        for y in range(h):
            for x in range(w):
                c = self._mask.pixelColor(x, y)
                if c.alpha() > 0 and (c.red(), c.green(), c.blue()) == (wr, wg, wb):
                    self._mask.setPixelColor(x, y, QColor(0, 0, 0, 0))
        self.update()

    def _hit_zone(self, pt):
        n = self._to_norm(pt)
        if not n:
            return -1
        def area(z):
            x0, y0, x1, y1 = z['rect']
            return abs(x1 - x0) * abs(y1 - y0)
        order = sorted(range(len(self.zones)),
                       key=lambda i: (0 if self.zones[i].get('type') == 'paint' else 1,
                                      area(self.zones[i])))
        for i in order:
            z = self.zones[i]
            x0, y0, x1, y1 = z['rect']
            if not (min(x0, x1) <= n[0] <= max(x0, x1) and min(y0, y1) <= n[1] <= max(y0, y1)):
                continue
            if z.get('type') == 'paint':
                px = self._to_pixel(pt)
                if not px or self._mask.isNull():
                    continue
                got = QColor(self._mask.pixelColor(px[0], px[1]))
                want = QColor(z.get('color', ''))
                if got.alpha() < 40 or (got.red(), got.green(), got.blue()) != (want.red(), want.green(), want.blue()):
                    continue
            return i
        return -1

    # ---- 鼠标交互 ----
    def mousePressEvent(self, e):
        if e.button() in (Qt.MiddleButton, Qt.RightButton):
            self._panning = True
            self._pan_start = e.position()
            self._pan_origin = QPointF(self._pan)
            self.setCursor(QCursor(Qt.ClosedHandCursor))
            return
        if e.button() != Qt.LeftButton:
            return
        if self.mode == 'rect':
            hit = self._hit_zone(e.position())
            if hit >= 0:
                self.selected = hit
                self.zoneClicked.emit(hit)
                self.update()
                return
            self._drag_start = e.position().toPoint()
            self._drag_cur = e.position().toPoint()
        else:
            self._drag_start = e.position().toPoint()
            self._drag_cur = e.position().toPoint()
            idx = self._color_zone(self.brush_color)
            if idx < 0:
                idx = self._new_paint_zone(self.brush_color)
            self.selected = idx
            self.zoneClicked.emit(idx)
            self._paint_at(e.position())
        self.update()

    def mouseMoveEvent(self, e):
        if self._panning and self._pan_start is not None:
            d = e.position() - self._pan_start
            self._pan = self._pan_origin + QPointF(d.x(), d.y())
            self.update()
            return
        if self._drag_start is not None:
            self._drag_cur = e.position().toPoint()
            if self.mode == 'paint':
                self._paint_at(e.position())
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() in (Qt.MiddleButton, Qt.RightButton):
            self._panning = False
            self._pan_start = None
            self.unsetCursor()
            return
        if e.button() != Qt.LeftButton:
            return
        if self.mode == 'rect' and self._drag_start is not None and self._drag_cur is not None:
            a = self._to_norm(self._drag_start); b = self._to_norm(self._drag_cur)
            if a and b:
                x0, y0 = min(a[0], b[0]), min(a[1], b[1])
                x1, y1 = max(a[0], b[0]), max(a[1], b[1])
                if x1 - x0 > 0.012 and y1 - y0 > 0.012:
                    self._new_rect_zone(x0, y0, x1, y1)
        self._drag_start = None
        self._drag_cur = None
        self.update()

    def wheelEvent(self, e):
        delta = e.angleDelta().y()
        if not delta:
            e.ignore(); return
        mouse = e.position()
        before = self._to_norm(mouse)
        self.zoom = max(0.25, min(6.0, self.zoom * (1.16 if delta > 0 else 1 / 1.16)))
        if before:
            g = self._view_geom()
            if g:
                _, _, dw, dh = g
                ax = mouse.x() - before[0] * dw
                ay = mouse.y() - before[1] * dh
                self._pan = QPointF(ax - (self.width() - dw) / 2, ay - (self.height() - dh) / 2)
        self.update(); e.accept()

    def _color_zone(self, color_hex):
        for i, z in enumerate(self.zones):
            if z.get('type') == 'paint' and z.get('color', '').lower() == color_hex.lower():
                return i
        return -1

    def _new_rect_zone(self, x0, y0, x1, y1):
        self.zones.append({
            "name": "区域%d" % (len(self.zones) + 1), "type": "rect",
            "rect": [round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)],
            "color": "", "speech": "", "reaction": "", "no_drag": False,
        })
        self.selected = len(self.zones) - 1
        self.zonesChanged.emit()
        self.zoneClicked.emit(self.selected)

    def _new_paint_zone(self, color_hex):
        self.zones.append({
            "name": "涂色%d" % (len(self.zones) + 1), "type": "paint",
            "rect": [0.0, 0.0, 1.0, 1.0], "color": color_hex,
            "speech": "", "reaction": "", "no_drag": False,
        })
        self.selected = len(self.zones) - 1
        self.zonesChanged.emit()
        return self.selected

    def delete_zone(self, idx):
        if 0 <= idx < len(self.zones):
            z = self.zones[idx]
            if z.get('type') == 'paint' and z.get('color'):
                self._erase_color(z['color'])
            self.zones.pop(idx)
            self.selected = -1
            self.zonesChanged.emit()
            self.update()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.update()


class NoScrollArea(QScrollArea):
    """隐藏滚动条但仍可用鼠标滚轮滚动的区域。"""
    def __init__(self):
        super().__init__()
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet("QScrollArea{background:transparent;}")

    def wheelEvent(self, e):
        bar = self.verticalScrollBar()
        delta = e.angleDelta().y() or e.pixelDelta().y()
        bar.setValue(bar.value() - delta)
        e.accept()


class WheelSpinBox(QSpinBox):
    """可通过全局开关决定是否响应滚轮的数值框。"""
    WHEEL_ENABLED = False

    def wheelEvent(self, e):
        if WheelSpinBox.WHEEL_ENABLED:
            super().wheelEvent(e)
        else:
            e.ignore()


class Builder(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("桌宠生成器 v2 - 把图片文件夹变成桌面宠物")
        self.setMinimumSize(1040, 680)
        self.resize(1220, 800)
        self.setStyleSheet(self.qss())
        self.worker = None
        self._editing_zone = False
        self._build_ui()
        self._load_builder_settings()

    def qss(self):
        return """
        QWidget{background:#16181d;color:#e8eaed;font:13px 'Microsoft YaHei';}
        QGroupBox{border:1px solid #333844;border-radius:8px;margin-top:10px;padding:12px;font-weight:bold;}
        QGroupBox::title{subcontrol-origin:margin;left:12px;padding:0 6px;color:#8ab4f8;}
        QLineEdit,QTextEdit,QSpinBox,QComboBox{background:#20232b;color:#e8eaed;border:1px solid #3a3f4b;
            border-radius:6px;padding:6px 8px;selection-background-color:#3a3f4b;}
        QLineEdit:focus,QTextEdit:focus{border:1px solid #7aa2e3;}
        QPushButton{background:#2a2e38;color:#e8eaed;border:1px solid #3a3f4b;border-radius:6px;padding:7px 14px;}
        QPushButton:hover{background:#343946;}
        QPushButton:pressed{background:#404656;}
        QPushButton#primary{background:#3a6ea5;border:1px solid #4a8fd0;font-weight:bold;font-size:14px;padding:10px;}
        QPushButton#primary:hover{background:#457fbd;}
        QPushButton#primary:disabled{background:#2a2e38;color:#6b7280;border:1px solid #3a3f4b;}
        QPushButton#danger{background:#5a2b2b;border:1px solid #8a4040;}
        QPushButton#danger:hover{background:#6e3535;}
        QPushButton#tool{background:#242833;border:1px solid #3a3f4b;padding:5px 9px;font:12px 'Microsoft YaHei';}
        QPushButton#tool:checked{background:#3a6ea5;border:1px solid #4a8fd0;}
        QLabel{color:#c8ccd4;}
        QLabel#hint{color:#7d8492;font-size:12px;}
        QCheckBox{color:#c8ccd4;spacing:6px;}
        QCheckBox::indicator{width:15px;height:15px;border:1px solid #3a3f4b;border-radius:4px;background:#20232b;}
        QCheckBox::indicator:checked{background:#3a6ea5;border:1px solid #4a8fd0;}
        QProgressBar{border:1px solid #3a3f4b;border-radius:6px;text-align:center;height:20px;background:#20232b;}
        QProgressBar::chunk{background:#4a8fd0;border-radius:5px;}
        QSplitter::handle{background:#333844;}
        """

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        title = QLabel("把任意序列帧图片文件夹，打包成会动的桌面宠物  ·  v2")
        title.setStyleSheet("font-size:18px;font-weight:bold;color:#f2f3f5;")
        root.addWidget(title)
        hint = QLabel("支持 png / jpg / webp / bmp / gif，按文件名自然排序作为动画顺序。透明背景效果最佳。")
        hint.setObjectName('hint'); root.addWidget(hint)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        root.addWidget(splitter, 1)

        # ---------- 左栏 ----------
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 8, 0); lv.setSpacing(8)

        g1 = QGroupBox("① 图片文件夹")
        l1 = QVBoxLayout(g1)
        row = QHBoxLayout()
        self.src_edit = QLineEdit(); self.src_edit.setPlaceholderText("选择存放序列帧的文件夹...")
        self.src_edit.setReadOnly(True)
        btn_src = QPushButton("选择文件夹"); btn_src.clicked.connect(self.choose_src)
        row.addWidget(self.src_edit, 1); row.addWidget(btn_src)
        l1.addLayout(row)
        self.info = QLabel("尚未选择文件夹 · 选好后可在下方预览图上框选或涂色")
        self.info.setObjectName('hint'); self.info.setWordWrap(True)
        l1.addWidget(self.info)
        lv.addWidget(g1)

        gprev = QGroupBox("② 预览 / 框选与涂色")
        lp = QVBoxLayout(gprev)
        # 先创建 preview（工具栏信号会用到它）
        self.preview = RegionPreview()
        self.preview.zoneClicked.connect(self.on_preview_zone_clicked)
        self.preview.zonesChanged.connect(self.on_zones_changed)
        tools = QHBoxLayout()
        self.mode_rect = QPushButton("▢ 框选"); self.mode_paint = QPushButton("◉ 涂色")
        for b in (self.mode_rect, self.mode_paint):
            b.setObjectName('tool'); b.setCheckable(True)
        self.mode_rect.setChecked(True)
        self.mode_rect.clicked.connect(lambda: self.set_mode('rect'))
        self.mode_paint.clicked.connect(lambda: self.set_mode('paint'))
        tools.addWidget(self.mode_rect); tools.addWidget(self.mode_paint)
        btn_reset = QPushButton("重置视图"); btn_reset.setObjectName('tool')
        btn_reset.clicked.connect(self.preview.reset_view)
        tools.addWidget(btn_reset); tools.addStretch(1)
        lp.addLayout(tools)

        self.paint_tools = QWidget()
        pt = QHBoxLayout(self.paint_tools); pt.setContentsMargins(0, 0, 0, 0)
        pt.addWidget(QLabel("颜色:"))
        self.color_btns = []
        for hexv, name in PALETTE:
            b = QToolButton(); b.setToolTip(name + " " + hexv); b.setFixedSize(24, 24)
            b.setStyleSheet("QToolButton{background:%s;border:2px solid #3a3f4b;border-radius:5px;}"
                            "QToolButton:checked{border:2px solid #ffffff;}" % hexv)
            b.setCheckable(True)
            b.clicked.connect(lambda _, h=hexv: self.choose_color(h))
            self.color_btns.append(b); pt.addWidget(b)
        self.color_btns[0].setChecked(True)
        pt.addWidget(QLabel("粗细:"))
        self.brush_spin = WheelSpinBox(); self.brush_spin.setRange(4, 80); self.brush_spin.setValue(18)
        self.brush_spin.valueChanged.connect(lambda v: setattr(self.preview, 'brush_size', v))
        pt.addWidget(self.brush_spin); pt.addStretch(1)
        self.paint_tools.setVisible(False)
        lp.addWidget(self.paint_tools)

        tip = QLabel("滚轮：以鼠标为中心缩放 · 中键/右键拖动：平移 · 框选：左键拖拽 · 涂色：选色后左键涂抹")
        tip.setObjectName('hint'); tip.setWordWrap(True)
        lp.addWidget(tip)
        lp.addWidget(self.preview, 1)
        lv.addWidget(gprev)
        splitter.addWidget(left)

        # ---------- 右栏 ----------
        right = NoScrollArea()
        inner = QWidget()
        rv = QVBoxLayout(inner); rv.setContentsMargins(8, 0, 0, 0); rv.setSpacing(8)

        g2 = QGroupBox("③ 基本信息")
        form = QFormLayout(g2)
        self.name_edit = QLineEdit("我的桌宠")
        form.addRow("桌宠名称\n（默认取文件夹名，可修改；也是 exe 文件名）", self.name_edit)
        self.speech_edit = QTextEdit(); self.speech_edit.setFixedHeight(88)
        self.speech_edit.setPlainText(DEFAULT_SPEECH)
        form.addRow("整体台词\n（点击空白处随机说一句）", self.speech_edit)
        rv.addWidget(g2)

        g_click = QGroupBox("④ 点击表现")
        fc = QVBoxLayout(g_click)
        lab = QLabel("点击桌宠时的动作（整体默认，可多选随机）："); lab.setObjectName('hint'); fc.addWidget(lab)
        self.react_checks = {}
        rr = QHBoxLayout()
        for text, val in GLOBAL_REACTIONS:
            cb = QCheckBox(text); cb.setChecked(val in ("blush", "jump"))
            self.react_checks[val] = cb; rr.addWidget(cb)
        fc.addLayout(rr); rv.addWidget(g_click)

        g_zone = QGroupBox("⑤ 事件区域 / 颜色事件表")
        fz = QFormLayout(g_zone)
        zsel = QHBoxLayout()
        self.zone_combo = QComboBox()
        self.zone_combo.currentIndexChanged.connect(self.on_zone_selected_index)
        zsel.addWidget(self.zone_combo, 1)
        btn_del = QPushButton("删除当前"); btn_del.setObjectName('danger')
        btn_del.clicked.connect(self.delete_zone); zsel.addWidget(btn_del)
        fz.addRow("区域（点预览图自动索引）", zsel)
        self.zone_name_edit = QLineEdit(); self.zone_name_edit.textChanged.connect(self.on_zone_field_changed)
        fz.addRow("区域名称", self.zone_name_edit)
        self.zone_speech_edit = QTextEdit(); self.zone_speech_edit.setFixedHeight(64)
        self.zone_speech_edit.setPlaceholderText("点击这个区域时说的话，每行一句；留空则随机用整体台词")
        self.zone_speech_edit.textChanged.connect(self.on_zone_field_changed)
        fz.addRow("专属台词", self.zone_speech_edit)
        self.zone_react_combo = QComboBox()
        for text, val in REACTION_OPTIONS:
            self.zone_react_combo.addItem(text, val)
        self.zone_react_combo.currentIndexChanged.connect(self.on_zone_field_changed)
        fz.addRow("点击动作", self.zone_react_combo)
        self.zone_nodrag = QCheckBox("此区域按下时不拖动宠物（用于脸/头等精细部位）")
        self.zone_nodrag.stateChanged.connect(self.on_zone_field_changed)
        fz.addRow(self.zone_nodrag)
        self.zone_hint = QLabel("在左栏框选或涂色后，这里编辑对应事件；颜色区域按色块区分")
        self.zone_hint.setObjectName('hint'); self.zone_hint.setWordWrap(True)
        fz.addRow(self.zone_hint)
        rv.addWidget(g_zone)

        g_hit = QGroupBox("⑥ 命中与拖拽")
        fh = QFormLayout(g_hit)
        self.hit_combo = QComboBox()
        self.hit_combo.addItem("框内即命中（宽松，推荐）", "any")
        self.hit_combo.addItem("仅命中角色不透明像素", "opaque")
        fh.addRow("命中判定", self.hit_combo)
        self.drag_th = WheelSpinBox(); self.drag_th.setRange(2, 30); self.drag_th.setValue(5)
        fh.addRow("拖拽灵敏度(px，越大越不易误拖)", self.drag_th)
        self.cb_region_drag = QCheckBox("启用“区域禁止拖拽”（配合上方每区勾选）")
        fh.addRow(self.cb_region_drag)
        rv.addWidget(g_hit)

        g_sw = QGroupBox("⑦ 交互开关")
        fs = QVBoxLayout(g_sw)
        self.cb_double = QCheckBox("允许双击隐藏 / 显示桌宠"); self.cb_double.setChecked(True)
        self.cb_edge = QCheckBox("启用贴边自动收纳（拖到屏幕边缘自动收起，鼠标靠近再弹出）"); self.cb_edge.setChecked(True)
        fs.addWidget(self.cb_double); fs.addWidget(self.cb_edge)
        rv.addWidget(g_sw)

        g_look = QGroupBox("⑧ 外观与动画")
        fl = QFormLayout(g_look)
        self.height_spin = WheelSpinBox(); self.height_spin.setRange(140, 1200); self.height_spin.setValue(420)
        self.fps = WheelSpinBox(); self.fps.setRange(1, 60); self.fps.setValue(24)
        self.target_h = QComboBox(); self.target_h.addItems(['700', '900', '1200', '1600', '保持原始尺寸']); self.target_h.setCurrentText('900')
        self.quality = WheelSpinBox(); self.quality.setRange(30, 100); self.quality.setValue(88)
        fl.addRow("显示高度(px)", self.height_spin)
        fl.addRow("播放帧率(fps)", self.fps)
        fl.addRow("资源清晰度", self.target_h)
        fl.addRow("图片质量(%)", self.quality)
        brow = QHBoxLayout()
        self.bt = QLineEdit("#ffe9ea"); self.bbg = QLineEdit("rgba(32,26,30,216)")
        self.bbd = QLineEdit("rgba(230,90,90,180)")
        self.fs = WheelSpinBox(); self.fs.setRange(9, 28); self.fs.setValue(13)
        for ed in (self.bt, self.bbg, self.bbd):
            btn = QPushButton("取色"); btn.clicked.connect(lambda _, e=ed: self.pick_color(e))
            h = QHBoxLayout(); h.addWidget(ed, 1); h.addWidget(btn); brow.addLayout(h)
        fl.addRow("气泡文字 / 背景 / 边框（CSS）", brow)
        fl.addRow("气泡字号", self.fs)
        rv.addWidget(g_look)

        g_set = QGroupBox("⑨ 生成器设置")
        fset = QFormLayout(g_set)
        self.cb_wheel = QCheckBox("允许滚轮调节数值框（默认关闭；开启后滚轮经过数字框才改值）")
        fset.addRow(self.cb_wheel)
        self.cb_remember = QCheckBox("记住上次配置（下次打开自动载入）"); self.cb_remember.setChecked(True)
        fset.addRow(self.cb_remember)
        self.cb_autoopen = QCheckBox("打包完成后自动打开输出文件夹"); self.cb_autoopen.setChecked(True)
        fset.addRow(self.cb_autoopen)
        rv.addWidget(g_set)

        g3 = QGroupBox("⑩ 输出位置")
        l3 = QVBoxLayout(g3)
        row3 = QHBoxLayout()
        self.out_edit = QLineEdit(default_out_dir()); self.out_edit.setReadOnly(True)
        btn_out = QPushButton("选择位置"); btn_out.clicked.connect(self.choose_out)
        row3.addWidget(self.out_edit, 1); row3.addWidget(btn_out)
        l3.addLayout(row3); rv.addWidget(g3)

        rv.addStretch()
        inner.setLayout(rv); right.setWidget(inner)
        splitter.addWidget(right); splitter.setSizes([430, 760])

        actrow = QHBoxLayout()
        self.build_btn = QPushButton("开始打包桌宠"); self.build_btn.setObjectName('primary')
        self.build_btn.clicked.connect(self.start_build)
        self.open_btn = QPushButton("打开输出文件夹"); self.open_btn.clicked.connect(self.open_out)
        actrow.addWidget(self.build_btn, 2); actrow.addWidget(self.open_btn, 1)
        root.addLayout(actrow)
        self.progress = QProgressBar(); self.progress.setValue(0); root.addWidget(self.progress)
        self.log = QTextEdit(); self.log.setReadOnly(True); self.log.setFixedHeight(78)
        self.log.setStyleSheet("background:#111318;color:#9aa0aa;border:1px solid #2a2e38;border-radius:6px;font:11px Consolas;")
        root.addWidget(self.log)

    # ---------- 生成器设置持久化 ----------
    def _load_builder_settings(self):
        s = BSETTINGS
        wheel = s.value('wheel_spin', False, type=bool)
        self.cb_wheel.setChecked(wheel); WheelSpinBox.WHEEL_ENABLED = wheel
        self.cb_remember.setChecked(s.value('remember', True, type=bool))
        self.cb_autoopen.setChecked(s.value('autoopen', True, type=bool))
        self.cb_wheel.stateChanged.connect(self._on_setting_changed)
        self.cb_remember.stateChanged.connect(self._on_setting_changed)
        self.cb_autoopen.stateChanged.connect(self._on_setting_changed)
        if self.cb_remember.isChecked():
            last = s.value('last_config', None)
            if isinstance(last, dict):
                self._apply_last_config(last)

    def _on_setting_changed(self):
        s = BSETTINGS
        WheelSpinBox.WHEEL_ENABLED = self.cb_wheel.isChecked()
        s.setValue('wheel_spin', self.cb_wheel.isChecked())
        s.setValue('remember', self.cb_remember.isChecked())
        s.setValue('autoopen', self.cb_autoopen.isChecked())
        s.sync()

    def _collect_config(self):
        return {
            'name': self.name_edit.text(), 'speech': self.speech_edit.toPlainText(),
            'height': self.height_spin.value(), 'fps': self.fps.value(),
            'target_h': self.target_h.currentText(), 'quality': self.quality.value(),
            'bubble_text': self.bt.text(), 'bubble_bg': self.bbg.text(), 'bubble_border': self.bbd.text(),
            'font_size': self.fs.value(),
            'reacts': [v for v, cb in self.react_checks.items() if cb.isChecked()],
            'hit_mode': self.hit_combo.currentData(), 'drag_th': self.drag_th.value(),
            'region_drag_block': self.cb_region_drag.isChecked(),
            'double': self.cb_double.isChecked(), 'edge': self.cb_edge.isChecked(),
            'src': self.src_edit.text(), 'out': self.out_edit.text(),
        }

    def _apply_last_config(self, c):
        try:
            self.name_edit.setText(c.get('name', ''))
            self.speech_edit.setPlainText(c.get('speech', DEFAULT_SPEECH))
            self.height_spin.setValue(int(c.get('height', 420)))
            self.fps.setValue(int(c.get('fps', 24)))
            self.target_h.setCurrentText(c.get('target_h', '900'))
            self.quality.setValue(int(c.get('quality', 88)))
            self.bt.setText(c.get('bubble_text', '#ffe9ea'))
            self.bbg.setText(c.get('bubble_bg', 'rgba(32,26,30,216)'))
            self.bbd.setText(c.get('bubble_border', 'rgba(230,90,90,180)'))
            self.fs.setValue(int(c.get('font_size', 13)))
            for v, cb in self.react_checks.items():
                cb.setChecked(v in c.get('reacts', []))
            idx = self.hit_combo.findData(c.get('hit_mode', 'any'))
            self.hit_combo.setCurrentIndex(idx if idx >= 0 else 0)
            self.drag_th.setValue(int(c.get('drag_th', 5)))
            self.cb_region_drag.setChecked(bool(c.get('region_drag_block', False)))
            self.cb_double.setChecked(bool(c.get('double', True)))
            self.cb_edge.setChecked(bool(c.get('edge', True)))
            if c.get('out') and os.path.isdir(c['out']):
                self.out_edit.setText(c['out'])
        except Exception:
            pass

    def closeEvent(self, e):
        if self.cb_remember.isChecked():
            BSETTINGS.setValue('last_config', self._collect_config()); BSETTINGS.sync()
        super().closeEvent(e)

    # ---------- 交互 ----------
    def set_mode(self, mode):
        self.preview.mode = mode
        self.mode_rect.setChecked(mode == 'rect')
        self.mode_paint.setChecked(mode == 'paint')
        self.paint_tools.setVisible(mode == 'paint')

    def choose_color(self, hexv):
        self.preview.brush_color = hexv
        for b, (h, _) in zip(self.color_btns, PALETTE):
            b.setChecked(h == hexv)

    def choose_src(self):
        d = QFileDialog.getExistingDirectory(self, "选择序列帧图片文件夹")
        if d:
            self.src_edit.setText(d); self.refresh_preview(d)

    def choose_out(self):
        d = QFileDialog.getExistingDirectory(self, "选择 exe 输出文件夹")
        if d:
            self.out_edit.setText(d)

    def refresh_preview(self, folder):
        files = image_files(folder)
        if not files:
            self.info.setText("未找到图片"); return
        img = QImage(files[0])
        if img.isNull():
            self.info.setText("首图无法读取"); return
        self.preview.set_image(files[0])
        self.preview.clear_zones()
        self.refresh_zone_combo()
        self.zone_hint.setText("在左栏框选或涂色后，这里编辑对应事件；颜色区域按色块区分")
        w, h = img.width(), img.height()
        self.info.setText("检测到 %d 帧 · 首帧 %d×%d · 宽高比 %.2f · 滚轮缩放/中键右键平移" % (len(files), w, h, w / h))
        folder_name = os.path.basename(os.path.normpath(folder))
        if not self.name_edit.text().strip() or self.name_edit.text() == "我的桌宠":
            self.name_edit.setText(folder_name + "桌宠")

    def pick_color(self, ed):
        c = QColorDialog.getColor(QColor(ed.text()) if QColor(ed.text()).isValid() else QColor('#ffffff'), self)
        if c.isValid():
            ed.setText(c.name())

    # ---- 区域下拉框 / 编辑（关键：斩断输入回写链，修文字前置 bug）----
    def refresh_zone_combo(self):
        self.zone_combo.blockSignals(True)
        self.zone_combo.clear()
        for i, z in enumerate(self.preview.zones):
            name = z.get('name', '区域')
            if z.get('type') == 'paint' and z.get('color'):
                name = "◉ " + name
            self.zone_combo.addItem("%d. %s" % (i + 1, name), i)
        sel = self.preview.selected
        if 0 <= sel < self.zone_combo.count():
            self.zone_combo.setCurrentIndex(sel)
        self.zone_combo.blockSignals(False)

    def on_zone_selected_index(self, idx):
        if idx < 0:
            return
        self.preview.selected = idx
        self.preview.update()
        self.load_zone_to_editor(idx)

    def on_preview_zone_clicked(self, idx):
        if 0 <= idx < self.zone_combo.count():
            self.zone_combo.setCurrentIndex(idx)
        self.load_zone_to_editor(idx)

    def load_zone_to_editor(self, row):
        zones = self.preview.zones
        if row < 0 or row >= len(zones):
            return
        z = zones[row]
        self._editing_zone = True
        self.zone_name_edit.setText(z.get('name', ''))
        self.zone_speech_edit.setPlainText(z.get('speech', ''))
        idx = self.zone_react_combo.findData(z.get('reaction', ''))
        self.zone_react_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.zone_nodrag.setChecked(bool(z.get('no_drag', False)))
        if z.get('type') == 'paint':
            self.zone_hint.setText("◉ 涂色区域：" + z.get('color', '') + " · 命中该颜色像素才触发")
        else:
            self.zone_hint.setText("▢ 框选区域：[%.2f,%.2f]-[%.2f,%.2f]" % tuple(z['rect']))
        self._editing_zone = False

    def on_zone_field_changed(self):
        if self._editing_zone:
            return
        row = self.preview.selected
        zones = self.preview.zones
        if row < 0 or row >= len(zones):
            return
        z = zones[row]
        z['name'] = self.zone_name_edit.text().strip() or ("区域%d" % (row + 1))
        z['speech'] = self.zone_speech_edit.toPlainText()
        z['reaction'] = self.zone_react_combo.currentData() or ""
        z['no_drag'] = self.zone_nodrag.isChecked()
        if 0 <= row < self.zone_combo.count():
            prefix = "◉ " if z.get('type') == 'paint' else ""
            self.zone_combo.setItemText(row, "%d. %s%s" % (row + 1, prefix, z['name']))
        self.preview.update()

    def on_zones_changed(self):
        self.refresh_zone_combo()
        sel = self.preview.selected
        if 0 <= sel < len(self.preview.zones):
            self.load_zone_to_editor(sel)

    def delete_zone(self):
        row = self.preview.selected
        if row < 0 or row >= len(self.preview.zones):
            QMessageBox.information(self, "提示", "请先选择一个区域")
            return
        self.preview.delete_zone(row)
        self.refresh_zone_combo()
        self.zone_hint.setText("在左栏框选或涂色后，这里编辑对应事件")

    # ---------- 打包 ----------
    def open_out(self):
        p = self.out_edit.text()
        if os.path.isdir(p):
            os.startfile(p)
        else:
            QMessageBox.information(self, "提示", "输出文件夹还不存在")

    def append_log(self, s):
        self.log.append(s)
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def start_build(self):
        src = self.src_edit.text()
        if not src or not os.path.isdir(src):
            QMessageBox.warning(self, "缺少素材", "请先选择序列帧图片文件夹"); return
        if not image_files(src):
            QMessageBox.warning(self, "缺少素材", "所选文件夹里没有图片"); return
        py = find_python()
        if not python_has_deps(py):
            QMessageBox.critical(self, "缺少打包依赖",
                "找不到同时安装了 PySide6 和 PyInstaller 的 Python 解释器。\n当前检测到：%s\n\n请先执行：\n  python -m pip install PySide6-Essentials PyInstaller" % (py or "（无）"))
            return
        target = self.target_h.currentText()
        target_h = None if target == '保持原始尺寸' else int(target)
        reacts = [v for v, cb in self.react_checks.items() if cb.isChecked()]
        zones = [{
            "name": z.get('name', '区域'), "type": z.get('type', 'rect'),
            "rect": z['rect'], "color": z.get('color', ''),
            "speech": [s for s in z.get('speech', '').splitlines() if s.strip()],
            "reaction": z.get('reaction', ''), "no_drag": bool(z.get('no_drag', False)),
        } for z in self.preview.zones]
        opts = dict(
            src_dir=src, name=safe_name(self.name_edit.text()), speech=self.speech_edit.toPlainText(),
            height=self.height_spin.value(), fps=self.fps.value(), target_h=target_h,
            quality=self.quality.value(), bubble_text=self.bt.text(), bubble_bg=self.bbg.text(),
            bubble_border=self.bbd.text(), font_size=self.fs.value(), click_reactions=reacts,
            double_click_hide=self.cb_double.isChecked(), edge_hide=self.cb_edge.isChecked(),
            regions=zones, hit_mode=self.hit_combo.currentData(),
            region_drag_block=self.cb_region_drag.isChecked(), drag_threshold=self.drag_th.value(),
            out_dir=self.out_edit.text(),
            mask_image=self.preview.mask_image(), has_paint=self.preview.has_paint(),
        )
        self.build_btn.setEnabled(False)
        self.progress.setValue(0); self.log.clear()
        self.worker = BuildWorker(opts)
        self.worker.log.connect(self.append_log)
        self.worker.progress.connect(self.progress.setValue)
        self.worker.done.connect(self.on_done)
        self.worker.failed.connect(self.on_failed)
        self.worker.start()

    def on_done(self, exe):
        self.build_btn.setEnabled(True); self.progress.setValue(100)
        if self.cb_remember.isChecked():
            BSETTINGS.setValue('last_config', self._collect_config()); BSETTINGS.sync()
        QMessageBox.information(self, "打包完成", "桌宠已生成：\n" + exe + "\n\n双击即可运行。")
        if self.cb_autoopen.isChecked():
            try:
                os.startfile(os.path.dirname(exe))
            except Exception:
                pass

    def on_failed(self, tb):
        self.build_btn.setEnabled(True)
        self.append_log("!!! 打包失败 !!!\n" + tb)
        QMessageBox.critical(self, "打包失败", tb[-1200:])


def main():
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    w = Builder()
    w.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
