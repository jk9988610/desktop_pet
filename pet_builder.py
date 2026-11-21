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
import zipfile
import traceback
import subprocess

from PySide6.QtCore import (
    Qt, QThread, Signal, QPoint, QPointF, QRect, QRectF, QEvent, QSettings, QByteArray, QBuffer, QTimer,
)
from PySide6.QtGui import (
    QImage, QPixmap, QColor, QFont, QPainter, QPen, QBrush, QCursor,
)
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QLineEdit, QTextEdit, QPushButton,
    QFileDialog, QInputDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
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
TEMPLATES_KEY = 'pet_templates_json'   # 多套设定模板（JSON: {名称: 配置}）
LAST_TEMPLATE_KEY = 'last_template_name'
MASK_MAX = 400   # 涂色掩膜长边上限（低分辨率足够命中判定，删除/涂色更快）


def mask_to_b64(mask):
    """把涂色掩膜 QImage 编码成 base64 字符串，便于随配置一起持久化。"""
    if mask is None or mask.isNull():
        return ''
    try:
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QBuffer.WriteOnly)
        mask.save(buf, 'PNG')
        buf.close()
        return bytes(ba.toBase64()).decode('ascii')
    except Exception:
        return ''


def b64_to_mask(s):
    """从 base64 字符串还原涂色掩膜 QImage。"""
    if not s:
        return None
    try:
        ba = QByteArray.fromBase64(s.encode('ascii'))
        img = QImage.fromData(bytes(ba))
        return img if not img.isNull() else None
    except Exception:
        return None


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

# 装饰插件槽位（与 pet.py SLOTS 保持一致）
PLUGIN_SLOTS = [
    ("feet", "脚底", [0.50, 0.94], 1.30, True),
    ("head", "头顶", [0.50, 0.04], 0.55, False),
    ("back", "身后", [0.50, 0.55], 1.10, True),
    ("front", "身前", [0.50, 0.60], 0.60, False),
]


def slot_info(slot):
    for s in PLUGIN_SLOTS:
        if s[0] == slot:
            return s
    return PLUGIN_SLOTS[0]


def natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', s)]


def image_files(folder):
    exts = ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif')
    files = [os.path.join(folder, f) for f in os.listdir(folder) if f.lower().endswith(exts)]
    return sorted(files, key=lambda p: natural_key(os.path.basename(p)))


def safe_name(name):
    return re.sub(r'[\\/:*?"<>|]+', '_', name).strip() or "桌宠"


def write_bytes_if_changed(path, data):
    """内容一致时不写盘，避免更新 mtime 破坏 PyInstaller 的 Analysis 缓存复用（快速增量模式依赖此特性）。"""
    try:
        with open(path, 'rb') as f:
            if f.read() == data:
                return False
    except OSError:
        pass
    with open(path, 'wb') as f:
        f.write(data)
    return True


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
            base = os.path.join(root, 'Programs', 'Python')
            if os.path.isdir(base):
                for d in sorted(os.listdir(base), reverse=True):
                    if d.lower().startswith('python3'):
                        p = os.path.join(base, d, 'python.exe')
                        if p not in candidates:
                            candidates.append(p)
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

    def __init__(self, opts, fast=False):
        super().__init__()
        self.opts = opts
        self.fast = bool(fast)

    def emit_log(self, s):
        self.log.emit(str(s))

    def run(self):
        o = self.opts
        try:
            sname = safe_name(o['name'])
            if self.fast:
                # 快速增量：工作目录按名字固定，保留 Analysis/PYZ 缓存供下次复用。
                # 单文件模式仍用时间戳目录，避免残留缓存干扰每次的干净构建。
                work = os.path.join(HERE, '_build', 'cache', sname)
            else:
                work = os.path.join(HERE, '_build', sname + '_' + str(int(time.time())))
            assets = os.path.join(work, 'assets')
            os.makedirs(assets, exist_ok=True)

            files = image_files(o['src_dir'])
            if not files:
                raise RuntimeError("所选文件夹里没有找到图片（支持 png/jpg/webp/bmp/gif）")
            self.emit_log("检测到 %d 张图片，开始处理..." % len(files))
            self.progress.emit(5)

            ratio = None
            kept_frames = set()
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
                frame_path = os.path.join(assets, 'frame_%04d.webp' % i)
                ba = QByteArray(); buf = QBuffer(ba)
                buf.open(QBuffer.WriteOnly)
                scaled.save(buf, 'WEBP', o['quality'])
                buf.close()
                write_bytes_if_changed(frame_path, bytes(ba.data()))
                kept_frames.add('frame_%04d.webp' % i)
                if (i + 1) % 10 == 0 or i == len(files) - 1:
                    self.emit_log("  已处理 %d/%d" % (i + 1, len(files)))
                    self.progress.emit(5 + int(35 * (i + 1) / len(files)))

            # 快速模式复用目录：删除本次不再需要的旧帧/旧掩膜，避免帧数减少时残留
            if self.fast:
                for old in os.listdir(assets):
                    if old not in kept_frames:
                        try:
                            os.remove(os.path.join(assets, old))
                        except Exception:
                            pass

            mask_obj = o.get('mask_image')
            if mask_obj is not None and not mask_obj.isNull() and o.get('has_paint'):
                ba = QByteArray(); buf = QBuffer(ba)
                buf.open(QBuffer.WriteOnly)
                mask_obj.save(buf, 'PNG')
                buf.close()
                write_bytes_if_changed(os.path.join(assets, 'paint_mask.png'), bytes(ba.data()))

            with open(os.path.join(template_dir(), 'pet.py'), 'rb') as pf:
                write_bytes_if_changed(os.path.join(work, 'pet.py'), pf.read())
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
            write_bytes_if_changed(os.path.join(work, 'config.json'),
                                   json.dumps(cfg, ensure_ascii=False, indent=2).encode('utf-8'))

            # 装饰插件：把每个插件文件夹压缩为 zip，输出到 exe 同目录的 plugins/。
            # 快速增量(onedir)的 exe 在「输出目录/名字/」内，插件要放进同一层。
            plugins = o.get('plugins', [])
            if plugins:
                if self.fast:
                    plug_out = os.path.join(o['out_dir'], sname, 'plugins')
                else:
                    plug_out = os.path.join(o['out_dir'], 'plugins')
                os.makedirs(plug_out, exist_ok=True)
                for idx, plug in enumerate(plugins):
                    if not plug.get('src_dir') or not os.path.isdir(plug['src_dir']):
                        self.emit_log("  跳过（文件夹不存在）: " + plug.get('name', '?'))
                        continue
                    pfiles = image_files(plug['src_dir'])
                    if not pfiles:
                        self.emit_log("  跳过空插件文件夹: " + plug.get('name', '?'))
                        continue
                    name = safe_name(plug.get('name') or ('装饰%d' % (idx + 1)))
                    meta = {
                        'name': plug.get('name') or ('装饰%d' % (idx + 1)),
                        'slot': plug.get('slot', 'feet'),
                        'anchor': plug.get('anchor', slot_info(plug.get('slot', 'feet'))[2]),
                        'scale': plug.get('scale', slot_info(plug.get('slot', 'feet'))[3]),
                        'behind': plug.get('behind', slot_info(plug.get('slot', 'feet'))[4]),
                        'fps': plug.get('fps', 12),
                    }
                    zip_path = os.path.join(plug_out, name + '.zip')
                    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                        zf.writestr('meta.json', json.dumps(meta, ensure_ascii=False, indent=2))
                        for k, src in enumerate(pfiles):
                            im = QImage(src)
                            if im.isNull():
                                continue
                            ba = QByteArray()
                            buf = QBuffer(ba)
                            buf.open(QBuffer.WriteOnly)
                            im.save(buf, 'WEBP', 85)
                            buf.close()
                            zf.writestr('frames/frame_%04d.webp' % k, bytes(ba.data()))
                    self.emit_log("  装饰插件已打包: %s (%d 帧)" % (os.path.basename(zip_path), len(pfiles)))
                self.emit_log("装饰插件输出到: " + plug_out)

            os.makedirs(o['out_dir'], exist_ok=True)
            sep = os.pathsep
            if self.fast:
                # 快速增量：onedir + 保留缓存。产物是「输出目录/名字/」文件夹。
                # 不清 --clean，PyInstaller 复用 workpath 里的 Analysis/PYZ。
                self.emit_log("快速增量模式：复用缓存打包（首次约需 1 分钟，之后仅数秒）...")
                target_exe = os.path.join(o['out_dir'], sname, sname + '.exe')
                free_target_exe(target_exe, sname + '.exe', self.emit_log)
                cmd = [
                    find_python(), '-m', 'PyInstaller', '--noconfirm', '--onedir', '--windowed',
                    '--name', sname,
                    '--add-data', 'assets' + sep + 'assets',
                    '--add-data', 'config.json' + sep + '.',
                    '--distpath', o['out_dir'],
                    '--workpath', os.path.join(work, 'work'),
                    '--specpath', work,
                    'pet.py',
                ]
            else:
                self.emit_log("单文件模式：开始打包（约需 30-90 秒）...")
                target_exe = os.path.join(o['out_dir'], sname + '.exe')
                free_target_exe(target_exe, sname + '.exe', self.emit_log)
                cmd = [
                    find_python(), '-m', 'PyInstaller', '--noconfirm', '--onefile', '--windowed', '--clean',
                    '--name', sname,
                    '--add-data', 'assets' + sep + 'assets',
                    '--add-data', 'config.json' + sep + '.',
                    '--distpath', o['out_dir'],
                    '--workpath', os.path.join(work, 'work'),
                    '--specpath', work,
                    'pet.py',
                ]
            self.progress.emit(42)
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

            exe = target_exe
            if not os.path.exists(exe):
                raise RuntimeError("未找到生成的 exe 文件（%s）" % exe)
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
    decoChanged = Signal(int)   # 预览里拖动/缩放某装饰后通知写回配置（插件索引）
    historyChanged = Signal()   # 撤销/重做栈变化（用于刷新按钮可用状态）

    def __init__(self):
        super().__init__()
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet("background:#20232b;border:1px solid #3a3f4b;border-radius:8px;")
        self.setMinimumSize(260, 420)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)
        self.setContextMenuPolicy(Qt.PreventContextMenu)
        self.setFocusPolicy(Qt.StrongFocus)   # 让 Ctrl+Z / Ctrl+Y 能落到预览上
        self._pm = QPixmap()
        self._mask = QImage()
        self.zones = []
        self.selected = -1
        # 撤销/重做：zones + 涂色掩膜 + 选中项的历史快照
        self._undo_stack = []
        self._redo_stack = []
        self._history_limit = 60
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
        # 装饰插件预览（与 pet.py 运行时同算法）
        self.deco_items = []      # [{pixmaps, slot, anchor, scale, behind, fps, dx, dy, vis}]
        self._deco_frame = 0
        self._deco_tick = QTimer(self)
        self._deco_tick.timeout.connect(self._advance_deco)
        self._deco_tick.start(80)
        # 逐图层变换（预览专用，不影响打包配置）：每个对象有 dx/dy(相对角色矩形宽高的比例) 与 scaleMul
        self.char_dx = 0.0; self.char_dy = 0.0; self.char_scale_mul = 1.0; self.char_vis = True
        self._sel = None          # ('char',) 或 ('deco', idx)
        self._move_drag = None    # (item, start_pos, start_dx, start_dy)
        self._char_playing = True
        self._deco_playing = True
        self._char_frames = []    # 角色动画帧（用于预览播放）
        self._char_frame = 0
        self._char_tick = QTimer(self)
        self._char_tick.timeout.connect(self._advance_char)
        self._char_tick.start(80)

    def set_decorations(self, plugins):
        """从生成器的插件状态装载装饰帧，用于预览。"""
        items = []
        for p in plugins or []:
            src = p.get('src_dir', '')
            if not src or not os.path.isdir(src):
                continue
            files = image_files(src)
            pixes = [QPixmap(f) for f in files]
            pixes = [pm for pm in pixes if not pm.isNull()]
            if not pixes:
                continue
            slot = p.get('slot', 'feet')
            info = slot_info(slot)
            items.append({
                'pixmaps': pixes,
                'slot': slot,
                'anchor': list(p.get('anchor', info[2])),
                'scale': float(p.get('scale', info[3])),
                'behind': bool(p.get('behind', info[4])),
                'fps': max(1, int(p.get('fps', 12))),
                'dx': 0.0, 'dy': 0.0,          # 预览位移（归一化到角色矩形）
                'scale_mul': 1.0,              # 预览缩放倍率
                'vis': True,                    # 该装饰是否显示/播放
            })
        # 尽量保留原有选中对象的预览变换（按插件索引对应）
        old = getattr(self, '_deco_items_by_idx', {})
        for i, it in enumerate(items):
            if i in old:
                it['dx'] = old[i].get('dx', 0.0); it['dy'] = old[i].get('dy', 0.0)
                it['scale_mul'] = old[i].get('scale_mul', 1.0)
                it['vis'] = old[i].get('vis', True)
        self.deco_items = items
        self._deco_items_by_idx = {i: it for i, it in enumerate(items)}
        if self._sel and self._sel[0] == 'deco' and self._sel[1] >= len(items):
            self._sel = None
        self._deco_frame = 0
        self.update()

    def _advance_deco(self):
        if self._deco_playing and (len(self.deco_items) > 1 or
                (self.deco_items and len(self.deco_items[0]['pixmaps']) > 1)):
            self._deco_frame += 1
            self.update()

    def _advance_char(self):
        if self._char_playing and len(self._char_frames) > 1:
            self._char_frame = (self._char_frame + 1) % len(self._char_frames)
            self.update()

    def set_char_frames(self, files):
        self._char_frames = [QPixmap(f) for f in files]
        self._char_frames = [pm for pm in self._char_frames if not pm.isNull()]
        self._char_frame = 0

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
        self.reset_history()   # 换素材 = 新工程，历史作废
        self.reset_view()

    def clear_zones(self):
        self.zones = []
        self.selected = -1
        if not self._mask.isNull():
            self._mask.fill(QColor(0, 0, 0, 0))
        self.reset_history()
        self.zonesChanged.emit()
        self.update()

    def mask_image(self):
        return self._mask

    def set_mask(self, path):
        """从已有 png 恢复涂色掩膜（导入已打包桌宠时用）。"""
        if not path or not os.path.isfile(path):
            return
        img = QImage(path)
        if not img.isNull():
            self.set_mask_image(img)

    def set_mask_image(self, img):
        """直接从一个 QImage 恢复涂色掩膜（记忆功能用）。"""
        if img is None or img.isNull():
            return
        self._mask = img.convertToFormat(QImage.Format_ARGB32)
        self.update()

    def set_zones(self, regions):
        """从配置 regions 恢复区域（导入已打包桌宠时用）。"""
        zones = []
        for r in regions or []:
            if not isinstance(r, dict) or 'rect' not in r:
                continue
            sp = r.get('speech', '')
            if isinstance(sp, list):
                sp = '\n'.join(sp)
            zones.append({
                'name': r.get('name', '区域'), 'type': r.get('type', 'rect'),
                'rect': list(r['rect']), 'color': r.get('color', ''),
                'speech': sp, 'reaction': r.get('reaction', ''),
                'no_drag': bool(r.get('no_drag', False)),
            })
        self.zones = zones
        self.selected = -1
        self.reset_history()
        self.update()

    def has_paint(self):
        return any(z.get('type') == 'paint' for z in self.zones)

    # ---- 撤销 / 重做（框选、涂色、删除）----
    def _snapshot(self):
        return {
            'zones': [{k: (list(v) if isinstance(v, list) else v) for k, v in z.items()}
                      for z in self.zones],
            'mask': self._mask.copy() if not self._mask.isNull() else QImage(),
            'selected': self.selected,
        }

    def _restore_snapshot(self, snap):
        self.zones = [{k: (list(v) if isinstance(v, list) else v) for k, v in z.items()}
                      for z in snap.get('zones', [])]
        m = snap.get('mask')
        self._mask = m.copy() if (m is not None and not m.isNull()) else QImage()
        self.selected = snap.get('selected', -1)
        self._drag_start = None
        self._drag_cur = None
        self.zonesChanged.emit()
        self.update()

    def push_history(self):
        """在会改变 zones/mask 的操作之前调用，记录当前状态。"""
        self._undo_stack.append(self._snapshot())
        if len(self._undo_stack) > self._history_limit:
            self._undo_stack.pop(0)
        self._redo_stack.clear()
        self.historyChanged.emit()

    def reset_history(self):
        self._undo_stack.clear()
        self._redo_stack.clear()
        self.historyChanged.emit()

    def can_undo(self):
        return bool(self._undo_stack)

    def can_redo(self):
        return bool(self._redo_stack)

    def undo(self):
        if not self._undo_stack:
            return
        self._redo_stack.append(self._snapshot())
        self._restore_snapshot(self._undo_stack.pop())
        self.historyChanged.emit()

    def redo(self):
        if not self._redo_stack:
            return
        self._undo_stack.append(self._snapshot())
        self._restore_snapshot(self._redo_stack.pop())
        self.historyChanged.emit()

    def reset_view(self):
        self.zoom = 1.0
        self._pan = QPointF(0, 0)
        self.char_dx = 0.0; self.char_dy = 0.0; self.char_scale_mul = 1.0
        for it in self.deco_items:
            it['dx'] = 0.0; it['dy'] = 0.0; it['scale_mul'] = 1.0
        self._sel = None
        self.update()

    def reset_layers(self):
        """重置角色与装饰的位移/缩放（不动视图缩放）。"""
        self.char_dx = 0.0; self.char_dy = 0.0; self.char_scale_mul = 1.0
        for it in self.deco_items:
            it['dx'] = 0.0; it['dy'] = 0.0; it['scale_mul'] = 1.0
        self._sel = None
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

    # ---- 图层变换 / 命中 ----
    def _char_disp_rect(self):
        """角色当前显示矩形（含预览变换）。"""
        g = self._view_geom()
        if not g:
            return None
        ox, oy, dw, dh = g
        cx = ox + (0.5 + self.char_dx) * dw
        cy = oy + (0.5 + self.char_dy) * dh
        w, h = dw * self.char_scale_mul, dh * self.char_scale_mul
        return (cx - w / 2, cy - h / 2, w, h)

    def _deco_disp_rect(self, idx):
        """某装饰当前显示矩形（含预览变换）。返回 (x,y,w,h,pm) 或 None。"""
        g = self._view_geom()
        if not g or idx < 0 or idx >= len(self.deco_items):
            return None
        it = self.deco_items[idx]
        ox, oy, dw, dh = g
        pm = it['pixmaps'][self._deco_frame % len(it['pixmaps'])]
        sc = it['scale'] * it['scale_mul']
        w = max(1, int(round(dw * sc)))
        h = max(1, int(round(w * pm.height() / max(1, pm.width()))))
        cx = ox + (it['anchor'][0] + it['dx']) * dw
        cy = oy + (it['anchor'][1] + it['dy']) * dh
        return (cx - w / 2, cy - h / 2, w, h, pm)

    def _alpha_hit(self, pm, rect, px, py, threshold=24):
        """矩形命中后再查像素 alpha，透明处不算命中。"""
        x, y, w, h = rect
        if px < x or px > x + w or py < y or py > y + h or w <= 0 or h <= 0:
            return False
        try:
            img = pm.toImage()
            ix = int((px - x) / w * max(0, img.width() - 1))
            iy = int((py - y) / h * max(0, img.height() - 1))
            return img.pixelColor(ix, iy).alpha() > threshold
        except Exception:
            return True

    def _layer_at(self, pt):
        """返回鼠标下的图层：('char',) 或 ('deco', idx) 或 None。
        装饰优先于角色（装饰更具体、更好选），前景优先于背景，后加的在上。"""
        px, py = pt.x(), pt.y()
        # 先按 z 序收集所有候选（前景装饰 → 背景装饰 → 角色）
        cands = []
        for i in range(len(self.deco_items) - 1, -1, -1):
            it = self.deco_items[i]
            if not it['vis']:
                continue
            r = self._deco_disp_rect(i)
            if r and self._alpha_hit(r[4], r[:4], px, py):
                # 前景优先（behind=False 排前面），同级保持 z 序
                cands.append((0 if not it['behind'] else 1, i))
        cr = self._char_disp_rect()
        if cr and self.char_vis and self._alpha_hit(self._pm, cr, px, py):
            cands.append((2, -1))
        if not cands:
            return None
        cands.sort(key=lambda c: c[0])
        return ('char',) if cands[0][1] < 0 else ('deco', cands[0][1])

    # ---- 绘制 ----
    def _draw_deco(self, p, behind):
        """按 pet.py 运行时同算法绘制装饰（叠加预览位移/缩放）：身后 → 身前。"""
        for i, it in enumerate(self.deco_items):
            if it['behind'] != behind or not it['vis']:
                continue
            r = self._deco_disp_rect(i)
            if not r:
                continue
            x, y, w, h, pm = r
            p.drawPixmap(int(round(x)), int(round(y)),
                         pm.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation))

    def _draw_sel_box(self, p):
        layer = self._sel
        if not layer:
            return
        if layer[0] == 'char':
            r = self._char_disp_rect()
            rect = QRect(int(r[0]), int(r[1]), int(r[2]), int(r[3])) if r else None
        else:
            rr = self._deco_disp_rect(layer[1])
            rect = QRect(int(rr[0]), int(rr[1]), int(rr[2]), int(rr[3])) if rr else None
        if rect:
            pen = QPen(QColor('#7ee787')); pen.setWidth(2); pen.setStyle(Qt.DashLine)
            p.setPen(pen); p.setBrush(QBrush(Qt.NoBrush))
            p.drawRect(rect.adjusted(-2, -2, 2, 2))

    def paintEvent(self, _):
        super().paintEvent(_)
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        g = self._view_geom()
        if g and not self._pm.isNull():
            ox, oy, dw, dh = g
            target = QRect(int(ox), int(oy), int(dw), int(dh))
            # z 序与桌宠运行时一致：身后装饰 → 角色 → 身前装饰
            self._draw_deco(p, behind=True)
            if self.char_vis:
                if self._char_playing and self._char_frames:
                    cm = self._char_frames[self._char_frame % len(self._char_frames)]
                    cr = self._char_disp_rect()
                    if cr:
                        rect = QRect(int(cr[0]), int(cr[1]), int(cr[2]), int(cr[3]))
                        p.drawPixmap(rect, cm.scaled(rect.width(), rect.height(),
                                                     Qt.IgnoreAspectRatio, Qt.SmoothTransformation))
                else:
                    cr = self._char_disp_rect()
                    rect = QRect(int(cr[0]), int(cr[1]), int(cr[2]), int(cr[3])) if cr else target
                    p.drawPixmap(rect, self._pm)
            self._draw_deco(p, behind=False)
            if not self._mask.isNull():
                p.setOpacity(0.72)
                p.drawImage(target, self._mask)
                p.setOpacity(1.0)
        self._draw_sel_box(p)
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
        # 涂色掩膜本质是「索引图」：每个有效像素必须精确等于某个调色板色（alpha=255）。
        # 关闭抗锯齿，避免边缘像素的 RGB 因预乘/混合产生偏差；用 Source 合成直接覆盖，
        # 避免与已有颜色叠加出「第三种颜色」。否则删除时按精确 RGB 匹配会留下彩色残粒。
        painter.setCompositionMode(QPainter.CompositionMode_Source)
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
        # 容忍旧版本抗锯齿/叠加造成的偏差像素：逐通道误差累计小于阈值即视为同色。
        # 调色板任意两色的曼哈顿距离最小约 90，阈值 45 不会误删相邻颜色。
        tol = 45
        changed = False
        for y in range(h):
            for x in range(w):
                c = self._mask.pixelColor(x, y)
                if c.alpha() <= 0:
                    continue
                # 极低 alpha 的半透明边（命中判定本就忽略）也一并清掉，避免视觉残粒
                near = (abs(c.red() - wr) + abs(c.green() - wg) + abs(c.blue() - wb)) <= tol
                if near or c.alpha() < 40:
                    self._mask.setPixelColor(x, y, QColor(0, 0, 0, 0))
                    changed = True
        if changed:
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
        # Ctrl+左键：选中并拖动图层（角色 / 装饰）
        if e.button() == Qt.LeftButton and (e.modifiers() & Qt.ControlModifier):
            layer = self._layer_at(e.position())
            self._sel = layer
            if layer:
                self._move_drag = (layer, e.position(),
                                   self.char_dx if layer[0] == 'char' else self.deco_items[layer[1]]['dx'],
                                   self.char_dy if layer[0] == 'char' else self.deco_items[layer[1]]['dy'])
                self.setCursor(QCursor(Qt.SizeAllCursor))
            self.update()
            return
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
            # 一次落笔 = 一条历史记录（在改动 zones/mask 之前压栈）
            self.push_history()
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
        if self._move_drag is not None:
            layer, start, sx, sy = self._move_drag
            g = self._view_geom()
            if g:
                dw, dh = g[2], g[3]
                ddx = (e.position().x() - start.x()) / max(1.0, dw)
                ddy = (e.position().y() - start.y()) / max(1.0, dh)
                it = None
                if layer[0] == 'char':
                    self.char_dx, self.char_dy = sx + ddx, sy + ddy
                else:
                    it = self.deco_items[layer[1]]
                    it['dx'], it['dy'] = sx + ddx, sy + ddy
                self.update()
            return
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
        if self._move_drag is not None and e.button() == Qt.LeftButton:
            layer = self._move_drag[0]
            self._move_drag = None
            self.unsetCursor()
            # 通知外部把预览位移/缩放写回配置（不在预览内改 anchor，避免重建时丢失）
            if layer[0] == 'deco' and layer[1] < len(self.deco_items):
                self.decoChanged.emit(layer[1])
            self.update()
            return
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

    def keyPressEvent(self, e):
        # Ctrl+Z 撤销 / Ctrl+Y 重做（Ctrl+Shift+Z 亦可重做）
        if e.modifiers() & Qt.ControlModifier:
            k = e.key()
            shift = bool(e.modifiers() & Qt.ShiftModifier)
            if k == Qt.Key_Z and not shift:
                self.undo(); e.accept(); return
            if k == Qt.Key_Y or (k == Qt.Key_Z and shift):
                self.redo(); e.accept(); return
        super().keyPressEvent(e)

    def wheelEvent(self, e):
        delta = e.angleDelta().y()
        if not delta:
            e.ignore(); return
        mouse = e.position()
        factor = 1.12 if delta > 0 else 1 / 1.12
        # Ctrl+滚轮：只缩放鼠标下的图层（角色 / 装饰），以鼠标为锚点
        if e.modifiers() & Qt.ControlModifier:
            layer = self._layer_at(mouse)
            g = self._view_geom()
            if layer and g:
                dw, dh = g[2], g[3]
                ox, oy = g[0], g[1]
                if layer[0] == 'char':
                    old = self.char_scale_mul
                    new = max(0.25, min(5.0, old * factor))
                    self.char_scale_mul = new
                    # 保持鼠标处不动：以鼠标为锚点反推中心位移
                    cx = ox + (0.5 + self.char_dx) * dw
                    cy = oy + (0.5 + self.char_dy) * dh
                    self.char_dx += (mouse.x() - cx) * (1 - new / old) / max(1.0, dw)
                    self.char_dy += (mouse.y() - cy) * (1 - new / old) / max(1.0, dh)
                    self._sel = ('char',)
                else:
                    it = self.deco_items[layer[1]]
                    old = it['scale_mul']
                    new = max(0.15, min(8.0, old * factor))
                    it['scale_mul'] = new
                    cx = ox + (it['anchor'][0] + it['dx']) * dw
                    cy = oy + (it['anchor'][1] + it['dy']) * dh
                    it['dx'] += (mouse.x() - cx) * (1 - new / old) / max(1.0, dw)
                    it['dy'] += (mouse.y() - cy) * (1 - new / old) / max(1.0, dh)
                    self._sel = layer
                    self.decoChanged.emit(layer[1])
                self.update(); e.accept(); return
        # 普通滚轮：以鼠标为中心缩放整个视图
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
        self.push_history()
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
            self.push_history()
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
        self._plugins_state = []   # 装饰插件列表：dict(src_dir,name,slot,anchor,scale,fps,behind)
        self._editing_plugin = False
        self._build_ui()
        self._load_builder_settings()   # 恢复设置 + 上次项目 + 窗口几何

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
        row2 = QHBoxLayout()
        btn_import = QPushButton("导入已打包的桌宠 exe")
        btn_import.setObjectName('tool')
        btn_import.setToolTip("从已打包的桌宠 exe 还原配置与素材，修改后可再次打包")
        btn_import.clicked.connect(self.import_pet)
        row2.addWidget(btn_import); row2.addStretch(1)
        l1.addLayout(row2)
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
        self.preview.decoChanged.connect(self.on_deco_preview_changed)
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
        tools.addWidget(btn_reset)
        btn_rst_layer = QPushButton("重置图层"); btn_rst_layer.setObjectName('tool')
        btn_rst_layer.setToolTip("把角色和装饰的拖动/缩放复位")
        btn_rst_layer.clicked.connect(self.preview.reset_layers)
        tools.addWidget(btn_rst_layer)
        self.btn_undo = QPushButton("↶ 撤销"); self.btn_undo.setObjectName('tool')
        self.btn_undo.setToolTip("撤销上一步框选/涂色/删除 (Ctrl+Z)")
        self.btn_undo.clicked.connect(self.preview.undo)
        self.btn_redo = QPushButton("↷ 重做"); self.btn_redo.setObjectName('tool')
        self.btn_redo.setToolTip("重做上一步撤销 (Ctrl+Y)")
        self.btn_redo.clicked.connect(self.preview.redo)
        self.preview.historyChanged.connect(self._update_history_buttons)
        tools.addWidget(self.btn_undo); tools.addWidget(self.btn_redo)
        tools.addStretch(1)
        self._update_history_buttons()
        lp.addLayout(tools)

        # 播放 / 显示开关（针对预览）
        playrow = QHBoxLayout()
        playrow.setContentsMargins(0, 0, 0, 0)
        self.cb_char_play = QCheckBox("▶ 角色动画"); self.cb_char_play.setChecked(True)
        self.cb_char_vis = QCheckBox("角色显示"); self.cb_char_vis.setChecked(True)
        self.cb_deco_play = QCheckBox("▶ 装饰动画"); self.cb_deco_play.setChecked(True)
        self.cb_deco_vis = QCheckBox("装饰显示"); self.cb_deco_vis.setChecked(True)
        for cb in (self.cb_char_play, self.cb_char_vis, self.cb_deco_play, self.cb_deco_vis):
            cb.setToolTip("仅影响预览，不改变打包结果")
            playrow.addWidget(cb)
        self.cb_char_play.toggled.connect(lambda v: setattr(self.preview, '_char_playing', v))
        self.cb_char_vis.toggled.connect(lambda v: setattr(self.preview, 'char_vis', v))
        self.cb_deco_play.toggled.connect(lambda v: setattr(self.preview, '_deco_playing', v))
        def _set_all_deco_vis(v):
            for it in self.preview.deco_items:
                it['vis'] = v
            self.preview.update()
        self.cb_deco_vis.toggled.connect(_set_all_deco_vis)
        playrow.addStretch(1)
        lp.addLayout(playrow)

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

        tip = QLabel("Ctrl+Z：撤销框选/涂色/删除 · Ctrl+Y：重做（也可点上方工具栏按钮）\n"
                     "Ctrl+滚轮：单独缩放角色/装饰 · Ctrl+左键拖动：移动角色/装饰 · 滚轮：缩放 · 中键/右键拖动：平移")
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
        self.name_edit.setToolTip("默认取文件夹名，可修改；也是生成的 exe 文件名")
        form.addRow("桌宠名称", self.name_edit)
        self.speech_edit = QTextEdit(); self.speech_edit.setFixedHeight(88)
        self.speech_edit.setPlainText(DEFAULT_SPEECH)
        self.speech_edit.setToolTip("点击桌宠空白处时随机说一句（每行一句）")
        form.addRow("整体台词", self.speech_edit)
        rv.addWidget(g2)

        # ---------- 多套设定模板 ----------
        g_tpl = QGroupBox("套装模板（可存多套，启动自动载入上次使用的）")
        ft = QVBoxLayout(g_tpl)
        row_tpl = QHBoxLayout()
        self.tpl_combo = QComboBox()
        self.tpl_combo.setToolTip("选择模板即载入该套设定；启动时自动定位到上次使用的模板")
        self.tpl_combo.currentIndexChanged.connect(self._on_template_combo_changed)
        row_tpl.addWidget(self.tpl_combo, 1)
        b_tpl_save = QPushButton("保存"); b_tpl_save.setObjectName('tool')
        b_tpl_save.setToolTip("把当前设定写回选中的模板")
        b_tpl_save.clicked.connect(self.save_template)
        b_tpl_saveas = QPushButton("另存为"); b_tpl_saveas.setObjectName('tool')
        b_tpl_saveas.clicked.connect(self.save_template_as)
        b_tpl_del = QPushButton("删除"); b_tpl_del.setObjectName('danger')
        b_tpl_del.clicked.connect(self.delete_template)
        row_tpl.addWidget(b_tpl_save); row_tpl.addWidget(b_tpl_saveas); row_tpl.addWidget(b_tpl_del)
        ft.addLayout(row_tpl)
        tpl_hint = QLabel("载入模板后所做的修改，会在打包/关闭时自动写回该模板；“另存为”可创建新的一套。")
        tpl_hint.setObjectName('hint'); tpl_hint.setWordWrap(True)
        ft.addWidget(tpl_hint)
        rv.addWidget(g_tpl)

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

        # 装饰插件（衣柜）
        g_plug = QGroupBox("装饰插件（衣柜 · 右键换装）")
        fp = QVBoxLayout(g_plug)
        plug_tip = QLabel("添加脚底光效等装饰序列帧文件夹，打包后压缩为 zip 放到 exe 同目录的 plugins\\，桌宠右键可换装。")
        plug_tip.setObjectName('hint'); plug_tip.setWordWrap(True)
        fp.addWidget(plug_tip)
        self.plug_list = QComboBox()
        self.plug_list.currentIndexChanged.connect(self.load_plugin_to_editor)
        fp.addWidget(self.plug_list)
        prow = QHBoxLayout()
        b_add = QPushButton("添加文件夹"); b_add.setObjectName('tool'); b_add.clicked.connect(self.add_plugin)
        b_del = QPushButton("移除"); b_del.setObjectName('tool'); b_del.clicked.connect(self.remove_plugin)
        prow.addWidget(b_add); prow.addWidget(b_del); prow.addStretch(1)
        fp.addLayout(prow)
        plug_form = QFormLayout()
        self.plug_name = QLineEdit()
        plug_form.addRow("名称", self.plug_name)
        self.plug_slot = QComboBox()
        for key, label, _a, _s, _b in PLUGIN_SLOTS:
            self.plug_slot.addItem(label, key)
        plug_form.addRow("槽位", self.plug_slot)
        self.plug_scale = WheelSpinBox(); self.plug_scale.setRange(5, 600); self.plug_scale.setValue(130)
        plug_form.addRow("缩放(%)", self.plug_scale)
        self.plug_fps = WheelSpinBox(); self.plug_fps.setRange(1, 60); self.plug_fps.setValue(12)
        plug_form.addRow("帧率(fps)", self.plug_fps)
        self.plug_ax = WheelSpinBox(); self.plug_ax.setRange(0, 100); self.plug_ax.setValue(50)
        self.plug_ay = WheelSpinBox(); self.plug_ay.setRange(0, 100); self.plug_ay.setValue(94)
        apos = QHBoxLayout(); apos.addWidget(self.plug_ax); apos.addWidget(QLabel("% X")); apos.addWidget(self.plug_ay); apos.addWidget(QLabel("% Y"))
        plug_form.addRow("锚点位置", apos)
        self.plug_behind = QCheckBox("画在角色身后（脚底/身后装饰请勾选）"); self.plug_behind.setChecked(True)
        plug_form.addRow(self.plug_behind)
        fp.addLayout(plug_form)
        for w in (self.plug_name, self.plug_slot, self.plug_scale, self.plug_fps, self.plug_ax, self.plug_ay, self.plug_behind):
            if isinstance(w, QCheckBox):
                w.stateChanged.connect(self.on_plugin_field_changed)
            elif isinstance(w, QComboBox):
                w.currentIndexChanged.connect(self.on_plugin_field_changed)
            else:
                w.textChanged.connect(self.on_plugin_field_changed) if isinstance(w, QLineEdit) else w.valueChanged.connect(self.on_plugin_field_changed)
        rv.addWidget(g_plug)

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
        self.bt = QLineEdit("#ffe9ea"); self.bbg = QLineEdit("rgba(32,26,30,216)")
        self.bbd = QLineEdit("rgba(230,90,90,180)")
        self.fs = WheelSpinBox(); self.fs.setRange(9, 28); self.fs.setValue(13)
        for label, ed in (("气泡文字色", self.bt), ("气泡背景色", self.bbg), ("气泡边框色", self.bbd)):
            btn = QPushButton("取色"); btn.clicked.connect(lambda _, e=ed: self.pick_color(e))
            h = QHBoxLayout(); h.setContentsMargins(0, 0, 0, 0)
            h.addWidget(ed, 1); h.addWidget(btn)
            fl.addRow(label, h)
        fl.addRow("气泡字号", self.fs)
        rv.addWidget(g_look)

        g_set = QGroupBox("⑨ 生成器设置")
        fset = QFormLayout(g_set)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("单文件 exe（便于分发）", "onefile")
        self.mode_combo.addItem("文件夹·快速增量（反复修改角色时更快）", "onedir")
        self.mode_combo.setToolTip(
            "单文件：生成一个独立 exe，方便分发，每次全量打包约 60-90 秒。\n"
            "文件夹·快速增量：生成文件夹，保留构建缓存；首次约 1 分钟，之后改角色仅数秒。")
        fset.addRow("打包模式", self.mode_combo)
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

        # 右栏容器：上=配置滚动区，底=按钮/进度/日志固定区（不随配置滚动）
        right_host = QWidget()
        rh = QVBoxLayout(right_host)
        rh.setContentsMargins(0, 0, 0, 0); rh.setSpacing(8)
        rh.addWidget(right, 1)

        actrow = QHBoxLayout()
        self.build_btn = QPushButton("开始打包桌宠"); self.build_btn.setObjectName('primary')
        self.build_btn.clicked.connect(self.start_build)
        self.open_btn = QPushButton("打开输出文件夹"); self.open_btn.clicked.connect(self.open_out)
        actrow.addWidget(self.build_btn, 2); actrow.addWidget(self.open_btn, 1)
        rh.addLayout(actrow)
        self.progress = QProgressBar(); self.progress.setValue(0); rh.addWidget(self.progress)
        self.log = QTextEdit(); self.log.setReadOnly(True); self.log.setFixedHeight(84)
        self.log.setStyleSheet("background:#111318;color:#9aa0aa;border:1px solid #2a2e38;border-radius:6px;font:11px Consolas;")
        rh.addWidget(self.log)

        splitter.addWidget(right_host); splitter.setSizes([716, 480])  # 预览区优先：左栏约 60%，右栏保证配置不被裁切

    # ---------- 多套设定模板 ----------
    def _load_templates(self):
        try:
            data = json.loads(BSETTINGS.value(TEMPLATES_KEY, '{}', type=str) or '{}')
            if isinstance(data, dict):
                return {str(k): v for k, v in data.items() if isinstance(v, dict)}
        except Exception:
            pass
        return {}

    def _save_templates(self):
        BSETTINGS.setValue(TEMPLATES_KEY, json.dumps(self._templates, ensure_ascii=False))
        BSETTINGS.sync()

    def _refresh_template_combo(self, select=None):
        self._tpl_loading = True
        self.tpl_combo.clear()
        names = sorted(self._templates.keys(), key=lambda x: x.lower())
        if names:
            self.tpl_combo.addItems(names)
            target = select if select in self._templates else names[0]
            idx = self.tpl_combo.findText(target)
            if idx >= 0:
                self.tpl_combo.setCurrentIndex(idx)
        else:
            self.tpl_combo.addItem("（暂无模板，点“另存为”创建）")
        self._tpl_loading = False

    def _on_template_combo_changed(self, _idx):
        if getattr(self, '_tpl_loading', False):
            return
        name = self.tpl_combo.currentText()
        cfg = self._templates.get(name)
        if not cfg:
            return
        self._apply_last_config(cfg)
        BSETTINGS.setValue(LAST_TEMPLATE_KEY, name)
        BSETTINGS.sync()
        self.append_log("已载入模板：" + name)

    def save_template(self):
        name = self.tpl_combo.currentText()
        if name not in self._templates:
            self.save_template_as()
            return
        cfg = self._collect_config()
        self._templates[name] = cfg
        self._save_templates()
        BSETTINGS.setValue(LAST_TEMPLATE_KEY, name)
        BSETTINGS.sync()
        self.append_log("模板已保存：" + name)

    def save_template_as(self):
        current = self.tpl_combo.currentText()
        default = current if current in self._templates else ""
        name, ok = QInputDialog.getText(self, "另存为模板", "模板名称：", text=default)
        if not ok:
            return
        name = name.strip()
        if not name:
            QMessageBox.warning(self, "名称为空", "请输入模板名称"); return
        if name in self._templates:
            ans = QMessageBox.question(
                self, "覆盖模板", "模板“%s”已存在，覆盖它吗？" % name,
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ans != QMessageBox.Yes:
                return
        cfg = self._collect_config()
        self._templates[name] = cfg
        self._save_templates()
        self._refresh_template_combo(name)
        BSETTINGS.setValue(LAST_TEMPLATE_KEY, name)
        BSETTINGS.sync()
        self.append_log("已另存模板：" + name)

    def delete_template(self):
        name = self.tpl_combo.currentText()
        if name not in self._templates:
            return
        ans = QMessageBox.question(
            self, "删除模板", "确定删除模板“%s”？此操作不可撤销。" % name,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ans != QMessageBox.Yes:
            return
        self._templates.pop(name, None)
        self._save_templates()
        if BSETTINGS.value(LAST_TEMPLATE_KEY, '', type=str) == name:
            BSETTINGS.remove(LAST_TEMPLATE_KEY)
        BSETTINGS.sync()
        self._refresh_template_combo()
        self.append_log("已删除模板：" + name)

    def _remember_template(self):
        """记录上次配置；若当前正使用某套模板，把修改自动写回该模板。"""
        if not self.cb_remember.isChecked():
            return
        cfg = self._collect_config()
        BSETTINGS.setValue('last_config', cfg)
        name = self.tpl_combo.currentText() if hasattr(self, 'tpl_combo') else ''
        if name in self._templates:
            self._templates[name] = cfg
            self._save_templates()
            BSETTINGS.setValue(LAST_TEMPLATE_KEY, name)
        BSETTINGS.sync()

    # ---------- 生成器设置持久化 ----------
    def _load_builder_settings(self):
        s = BSETTINGS
        wheel = s.value('wheel_spin', False, type=bool)
        self.cb_wheel.setChecked(wheel); WheelSpinBox.WHEEL_ENABLED = wheel
        self.cb_remember.setChecked(s.value('remember', True, type=bool))
        self.cb_autoopen.setChecked(s.value('autoopen', True, type=bool))
        build_mode = s.value('build_mode', 'onefile')
        idx = self.mode_combo.findData(build_mode)
        self.mode_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.cb_wheel.stateChanged.connect(self._on_setting_changed)
        self.cb_remember.stateChanged.connect(self._on_setting_changed)
        self.cb_autoopen.stateChanged.connect(self._on_setting_changed)
        self.mode_combo.currentIndexChanged.connect(self._on_setting_changed)

        # 窗口大小 / 位置（与"记住配置"开关无关，始终恢复）
        try:
            ww = s.value('win_w', 0, type=int)
            wh = s.value('win_h', 0, type=int)
            if ww >= self.minimumWidth() and wh >= self.minimumHeight():
                self.resize(ww, wh)
            px = s.value('win_x', None)
            py = s.value('win_y', None)
            if px is not None and py is not None:
                self.move(int(px), int(py))
        except Exception:
            pass

        # 套装模板：默认自动载入上次使用的那一套，没有模板时才回退到上次配置
        self._templates = self._load_templates()
        self._refresh_template_combo()
        if self.cb_remember.isChecked():
            last_tpl = s.value(LAST_TEMPLATE_KEY, '', type=str)
            if last_tpl in self._templates:
                self._apply_last_config(self._templates[last_tpl])
                self._tpl_loading = True
                idx = self.tpl_combo.findText(last_tpl)
                if idx >= 0:
                    self.tpl_combo.setCurrentIndex(idx)
                self._tpl_loading = False
                self.append_log("已自动载入上次使用的模板：" + last_tpl)
            else:
                last = s.value('last_config', None)
                if isinstance(last, dict):
                    self._apply_last_config(last)

    def _on_setting_changed(self):
        s = BSETTINGS
        WheelSpinBox.WHEEL_ENABLED = self.cb_wheel.isChecked()
        s.setValue('wheel_spin', self.cb_wheel.isChecked())
        s.setValue('remember', self.cb_remember.isChecked())
        s.setValue('autoopen', self.cb_autoopen.isChecked())
        s.setValue('build_mode', self.mode_combo.currentData())
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
            'plugins': [dict(p) for p in self._plugins_state],
            'regions': [dict(z) for z in self.preview.zones],
            'mask_b64': mask_to_b64(self.preview.mask_image()),
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

            # 恢复"上次打开的项目"：素材源文件夹 + 装饰
            src = c.get('src', '')
            if src and os.path.isdir(src) and image_files(src):
                self.src_edit.setText(src)
                self.refresh_preview(src)   # 会 clear_zones，故区域/掩膜须在其后恢复
            plugs = c.get('plugins', [])
            if isinstance(plugs, list):
                self._plugins_state = [dict(p) for p in plugs
                                       if isinstance(p, dict) and p.get('src_dir')
                                       and os.path.isdir(p['src_dir'])]
            self.refresh_plugin_list()

            # 区域 + 涂色掩膜（必须在 refresh_preview 之后）
            regions = c.get('regions', [])
            if isinstance(regions, list) and regions:
                self.preview.set_zones(regions)
            mimg = b64_to_mask(c.get('mask_b64', ''))
            if mimg is not None:
                self.preview.set_mask_image(mimg)
            self.refresh_zone_combo()
            self._sync_preview_decos()
        except Exception:
            pass

    def closeEvent(self, e):
        # 窗口大小 / 位置始终记忆（与"记住配置"开关无关）
        try:
            BSETTINGS.setValue('win_w', self.width())
            BSETTINGS.setValue('win_h', self.height())
            BSETTINGS.setValue('win_x', self.x())
            BSETTINGS.setValue('win_y', self.y())
        except Exception:
            pass
        self._remember_template()
        BSETTINGS.sync()
        super().closeEvent(e)

    # ---------- 交互 ----------
    def _update_history_buttons(self):
        self.btn_undo.setEnabled(self.preview.can_undo())
        self.btn_redo.setEnabled(self.preview.can_redo())

    def keyPressEvent(self, e):
        # 焦点在按钮/窗口等非文本控件时，Ctrl+Z / Ctrl+Y 也作用于区域编辑。
        # 文本框/编辑器会自行处理并消费该按键（保留原生文字撤销），不会传播到这里。
        if e.modifiers() & Qt.ControlModifier:
            k = e.key()
            shift = bool(e.modifiers() & Qt.ShiftModifier)
            if k == Qt.Key_Z and not shift:
                self.preview.undo(); e.accept(); return
            if k == Qt.Key_Y or (k == Qt.Key_Z and shift):
                self.preview.redo(); e.accept(); return
        super().keyPressEvent(e)

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

    def _extract_helper_path(self):
        """定位解包助手脚本（源码 / frozen 两种模式）。"""
        if hasattr(sys, '_MEIPASS'):
            p = os.path.join(sys._MEIPASS, 'pet_extract.py')
            if os.path.isfile(p):
                return p
        return os.path.join(app_dir(), 'pet_extract.py')

    def import_pet(self):
        """从已打包的桌宠 exe 还原配置与素材到生成器，便于修改后再次打包。"""
        exe, _ = QFileDialog.getOpenFileName(self, "选择已打包的桌宠 exe", "", "桌宠 exe (*.exe)")
        if not exe:
            return
        helper = self._extract_helper_path()
        if not os.path.isfile(helper):
            QMessageBox.critical(self, "缺少组件", "找不到解包助手 pet_extract.py"); return
        py = find_python()
        if not py:
            QMessageBox.critical(self, "缺少 Python", "找不到可用的 Python 解释器"); return

        # 解包到桌宠同目录下的 _还原工程 文件夹（稳定，避免临时目录被清理）
        exe_dir = os.path.dirname(exe)
        proj = os.path.join(exe_dir, '_还原工程_' + os.path.splitext(os.path.basename(exe))[0])
        try:
            os.makedirs(proj, exist_ok=True)
        except Exception as e:
            QMessageBox.critical(self, "无法创建工程目录", str(e)); return
        self.append_log("正在从桌宠 exe 还原工程: " + exe)

        import subprocess as _sp
        cmd = [py, helper, exe, proj]
        env = os.environ.copy(); env['PYTHONIOENCODING'] = 'utf-8'; env['PYTHONUTF8'] = '1'
        try:
            proc = _sp.run(cmd, capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=120, env=env,
                           creationflags=getattr(_sp, 'CREATE_NO_WINDOW', 0))
            out = (proc.stdout or '').strip()
            # 取最后一行 JSON
            line = out.splitlines()[-1] if out else ''
            m = json.loads(line)
        except Exception as e:
            QMessageBox.critical(self, "解包失败", "无法运行解包助手：\n%s\n\n%s" % (e, (proc.stderr or '')[-500:] if 'proc' in dir() else ''))
            return
        if not m.get('ok'):
            QMessageBox.critical(self, "解包失败", m.get('error', '未知错误')[-1200:]); return

        cfg = m.get('config', {})
        assets = m.get('assets_dir', '')
        if not assets or not os.path.isdir(assets) or not image_files(assets):
            QMessageBox.critical(self, "解包失败", "没能在 exe 里找到素材帧"); return

        # ---- 回填素材与预览 ----
        self.src_edit.setText(assets)
        self.refresh_preview(assets)
        self.name_edit.setText(cfg.get('name', os.path.splitext(os.path.basename(exe))[0]))

        # ---- 台词 / 点击反应 / 外观 ----
        speech = cfg.get('speech', [])
        self.speech_edit.setPlainText('\n'.join(speech) if isinstance(speech, list) else str(speech))
        reacts = cfg.get('click_reactions', [])
        for v, cb in self.react_checks.items():
            cb.setChecked(v in reacts)
        self.height_spin.setValue(int(cfg.get('default_height', 420)))
        self.fps.setValue(int(cfg.get('default_fps', 24)))
        self.bt.setText(cfg.get('bubble_text', '#ffe9ea'))
        self.bbg.setText(cfg.get('bubble_bg', 'rgba(32,26,30,216)'))
        self.bbd.setText(cfg.get('bubble_border', 'rgba(230,90,90,180)'))
        self.fs.setValue(int(cfg.get('bubble_font_size', 13)))
        self.cb_double.setChecked(bool(cfg.get('double_click_hide', True)))
        self.cb_edge.setChecked(bool(cfg.get('edge_hide', True)))
        hi = self.hit_combo.findData(cfg.get('hit_mode', 'any'))
        self.hit_combo.setCurrentIndex(hi if hi >= 0 else 0)
        self.drag_th.setValue(int(cfg.get('drag_threshold', 5)))
        self.cb_region_drag.setChecked(bool(cfg.get('region_drag_block', False)))

        # 清晰度：按解包帧高度推断
        first = QImage(image_files(assets)[0])
        fh = first.height() if not first.isNull() else 0
        opts = [700, 900, 1200, 1600]
        nearest = min(opts, key=lambda x: abs(x - fh)) if fh else 900
        if fh and abs(nearest - fh) <= 80:
            self.target_h.setCurrentText(str(nearest))
        else:
            self.target_h.setCurrentText('保持原始尺寸')

        # ---- 区域 / 涂色掩膜 ----
        self.preview.set_zones(cfg.get('regions', []))
        if m.get('mask_path'):
            self.preview.set_mask(m['mask_path'])
        self.refresh_zone_combo()

        # ---- 装饰插件 ----
        plugs = []
        for p in m.get('plugins', []):
            if p.get('_error') or not p.get('_src_dir'):
                continue
            if not os.path.isdir(p['_src_dir']) or not image_files(p['_src_dir']):
                continue
            plugs.append({
                'src_dir': p['_src_dir'], 'name': p.get('name', p.get('_zip_name', '装饰')),
                'slot': p.get('slot', 'feet'), 'anchor': list(p.get('anchor', [0.5, 0.94])),
                'scale': float(p.get('scale', 1.3)), 'fps': int(p.get('fps', 12)),
                'behind': bool(p.get('behind', True)),
            })
        self._plugins_state = plugs
        self.refresh_plugin_list()
        self._sync_preview_decos()
        if plugs:
            self.plug_list.setCurrentIndex(0)

        # 输出位置设为 exe 所在目录，方便覆盖还原
        self.out_edit.setText(exe_dir)

        self.append_log("还原完成：%d 帧 · %d 区域 · %d 装饰 → 可修改后重新打包" %
                        (m.get('frame_count', 0), len(cfg.get('regions', [])), len(plugs)))
        self.append_log("工程目录: " + proj)
        QMessageBox.information(self, "导入成功",
            "已还原桌宠「%s」的全部设计与素材。\n\n现在可以修改名称/台词/区域/装饰等，然后点“开始打包桌宠”重新生成。\n\n注意：素材帧来自已压缩的 exe，二次打包画质可能略有下降。" % cfg.get('name', ''))

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
        self.preview.set_char_frames(files)
        self._sync_preview_decos()
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

    # ---------- 装饰插件管理 ----------
    def _sync_preview_decos(self):
        """把当前插件状态同步到预览区。"""
        self.preview.set_decorations(self._plugins_state)

    def on_deco_preview_changed(self, idx):
        """预览里拖动/缩放装饰后，把 effective anchor/scale 写回插件配置与编辑器字段。"""
        if idx < 0 or idx >= len(self.preview.deco_items) or idx >= len(self._plugins_state):
            return
        it = self.preview.deco_items[idx]
        ax = it['anchor'][0] + it['dx']
        ay = it['anchor'][1] + it['dy']
        sc = it['scale'] * it['scale_mul']
        # 归一化锚点，限制在合理范围
        ax = max(-0.5, min(1.5, ax)); ay = max(-0.5, min(1.5, ay))
        sc = max(0.05, min(6.0, sc))
        it['anchor'] = [ax, ay]; it['scale'] = sc
        it['dx'] = 0.0; it['dy'] = 0.0; it['scale_mul'] = 1.0
        p = self._plugins_state[idx]
        p['anchor'] = [ax, ay]; p['scale'] = sc
        # 同步到编辑器控件（只在该插件正被编辑时）
        if self.plug_list.currentIndex() == idx:
            self._editing_plugin = True
            self.plug_scale.setValue(int(round(sc * 100)))
            self.plug_ax.setValue(int(round(ax * 100)))
            self.plug_ay.setValue(int(round(ay * 100)))
            self._editing_plugin = False
        self.plug_list.blockSignals(True)
        self.refresh_plugin_list()
        self.plug_list.setCurrentIndex(idx)
        self.plug_list.blockSignals(False)
        self.append_log("预览调整了装饰「%s」：锚点 (%.2f, %.2f)，缩放 %.0f%%" %
                        (p.get('name', '?'), ax, ay, sc * 100))

    def refresh_plugin_list(self):
        self.plug_list.blockSignals(True)
        self.plug_list.clear()
        for i, p in enumerate(self._plugins_state):
            folder = os.path.basename(os.path.normpath(p.get('src_dir', '')))
            self.plug_list.addItem("%d. %s  [%s]  %s" % (
                i + 1, p.get('name', '?'), slot_info(p.get('slot', 'feet'))[1], folder), i)
        self.plug_list.blockSignals(False)

    def add_plugin(self):
        d = QFileDialog.getExistingDirectory(self, "选择装饰序列帧文件夹（如：地面 脚底光效）")
        if not d:
            return
        imgs = image_files(d)
        if not imgs:
            QMessageBox.warning(self, "没有图片", "该文件夹里没有找到图片（png/jpg/webp/bmp/gif）")
            return
        folder_name = os.path.basename(os.path.normpath(d))
        # 根据文件夹名猜槽位
        slot = 'feet'
        if any(k in folder_name for k in ('头顶', '头饰', '光环', 'head')):
            slot = 'head'
        elif any(k in folder_name for k in ('身后', '翅膀', '背', 'back', 'wing')):
            slot = 'back'
        elif any(k in folder_name for k in ('身前', '手持', 'front')):
            slot = 'front'
        _key, _label, anchor, scale, behind = slot_info(slot)
        self._plugins_state.append({
            'src_dir': d, 'name': folder_name, 'slot': slot,
            'anchor': list(anchor), 'scale': scale, 'fps': 12, 'behind': behind,
        })
        self.refresh_plugin_list()
        self.plug_list.setCurrentIndex(len(self._plugins_state) - 1)
        self._sync_preview_decos()
        self.append_log("已添加装饰插件: %s（%d 帧，%s）— 预览区应已显示" % (folder_name, len(imgs), _label))

    def remove_plugin(self):
        idx = self.plug_list.currentIndex()
        if idx < 0 or idx >= len(self._plugins_state):
            QMessageBox.information(self, "提示", "请先选择一个装饰插件")
            return
        self._plugins_state.pop(idx)
        self.refresh_plugin_list()
        self._sync_preview_decos()
        if self._plugins_state:
            self.plug_list.setCurrentIndex(0)
            self.load_plugin_to_editor(0)

    def load_plugin_to_editor(self, idx):
        if idx < 0 or idx >= len(self._plugins_state):
            return
        p = self._plugins_state[idx]
        self._editing_plugin = True
        self.plug_name.setText(p.get('name', ''))
        si = self.plug_slot.findData(p.get('slot', 'feet'))
        self.plug_slot.setCurrentIndex(si if si >= 0 else 0)
        self.plug_scale.setValue(int(round(p.get('scale', 1.0) * 100)))
        self.plug_fps.setValue(int(p.get('fps', 12)))
        a = p.get('anchor', [0.5, 0.94])
        self.plug_ax.setValue(int(round(a[0] * 100)))
        self.plug_ay.setValue(int(round(a[1] * 100)))
        self.plug_behind.setChecked(bool(p.get('behind', True)))
        self._editing_plugin = False

    def on_plugin_field_changed(self, *args):
        if getattr(self, '_editing_plugin', False):
            return
        idx = self.plug_list.currentIndex()
        if idx < 0 or idx >= len(self._plugins_state):
            return
        p = self._plugins_state[idx]
        p['name'] = self.plug_name.text().strip() or ('装饰%d' % (idx + 1))
        p['slot'] = self.plug_slot.currentData() or 'feet'
        p['scale'] = self.plug_scale.value() / 100.0
        p['fps'] = self.plug_fps.value()
        p['anchor'] = [self.plug_ax.value() / 100.0, self.plug_ay.value() / 100.0]
        p['behind'] = self.plug_behind.isChecked()
        self.refresh_plugin_list()
        self.plug_list.blockSignals(True)
        self.plug_list.setCurrentIndex(idx)
        self.plug_list.blockSignals(False)
        self._sync_preview_decos()

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
            plugins=[dict(p) for p in self._plugins_state],
        )
        self.build_btn.setEnabled(False)
        self.progress.setValue(0); self.log.clear()
        fast = (self.mode_combo.currentData() == 'onedir')
        self._last_fast = fast
        self.worker = BuildWorker(opts, fast=fast)
        self.worker.log.connect(self.append_log)
        self.worker.progress.connect(self.progress.setValue)
        self.worker.done.connect(self.on_done)
        self.worker.failed.connect(self.on_failed)
        self.worker.start()

    def on_done(self, exe):
        self.build_btn.setEnabled(True); self.progress.setValue(100)
        self._remember_template()
        BSETTINGS.sync()
        if getattr(self, '_last_fast', False):
            hint = ("文件夹·快速增量模式：桌宠已生成在下方文件夹。\n" + os.path.dirname(exe) +
                    "\n\n请运行其中的「" + os.path.basename(exe) + "」。\n"
                    "注意：文件夹需整体保留，不要单独移动 exe。")
        else:
            hint = "桌宠已生成：\n" + exe + "\n\n双击即可运行。"
        QMessageBox.information(self, "打包完成", hint)
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
