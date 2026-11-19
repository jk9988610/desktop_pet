# -*- coding: utf-8 -*-
"""
通用桌面宠物运行时（配置驱动）v2
从同目录的 config.json 读取桌宠名称、帧数、台词、外观、点击区域等配置。
无边框透明置顶；拖拽移动、单击互动、右键菜单、双击隐藏、贴边收纳、系统托盘。

v2 区域模型（regions）：
  {name, type:'rect'|'paint', rect:[x0,y0,x1,y1]归一化, color:'#rrggbb'(paint用),
   speech:[...], reaction:'...', no_drag:bool}
  - rect：矩形区域；paint：命中 paint_mask 中对应 color 的像素才算
  - 命中优先级：paint > 面积小的 rect > 面积大的 rect；可选仅命中不透明像素
  - no_drag：该区域按下不触发拖拽（配合 region_drag_block 总开关）
兼容旧配置 click_zones（自动转为 rect 区域）。
"""
import os
import sys
import json
import random
import hashlib

from PySide6.QtCore import (
    Qt, QTimer, QPoint, QPropertyAnimation, QEasingCurve, QSettings,
)
from PySide6.QtGui import (
    QPixmap, QImage, QColor, QCursor, QPainter, QFont, QFontMetrics, QGuiApplication, QIcon,
)
from PySide6.QtWidgets import (
    QApplication, QWidget, QMenu, QSystemTrayIcon, QLabel,
)


# ---------- 读取配置 ----------
DEFAULTS = {
    "name": "桌宠",
    "frame_count": 1,
    "default_height": 420,
    "default_fps": 24,
    "speech": ["你好呀~", "今天也要开心哦！", "（歪头看着你）"],
    "bubble_text": "#ffe9ea",
    "bubble_bg": "rgba(32,26,30,216)",
    "bubble_border": "rgba(230,90,90,180)",
    "bubble_font_size": 13,
    "click_reactions": ["blush", "jump", "wink", "spin"],
    "double_click_hide": True,
    "edge_hide": True,
    "regions": [],
    # 命中策略：any=框内即命中；opaque=仅命中不透明像素
    "hit_mode": "any",
    # 区域禁拖总开关 / 拖拽位移阈值(px)
    "region_drag_block": False,
    "drag_threshold": 5,
}

REACTIONS = ["blush", "jump", "wink", "spin"]


def _to_bool(v, default=False):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def load_config():
    path = resource_path("config.json")
    cfg = dict(DEFAULTS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            user = json.load(f)
        if isinstance(user, dict):
            cfg.update(user)
    except Exception:
        pass
    try:
        cfg["frame_count"] = max(1, int(cfg["frame_count"]))
    except Exception:
        cfg["frame_count"] = 1
    for k in ("default_height", "default_fps", "bubble_font_size"):
        try:
            cfg[k] = int(cfg[k])
        except Exception:
            cfg[k] = DEFAULTS[k]
    if not isinstance(cfg.get("speech"), list) or not cfg["speech"]:
        cfg["speech"] = DEFAULTS["speech"]
    cfg["speech"] = [str(s) for s in cfg["speech"]]

    for k in ("double_click_hide", "edge_hide", "region_drag_block"):
        cfg[k] = _to_bool(cfg.get(k, DEFAULTS[k]), DEFAULTS[k])
    try:
        cfg["drag_threshold"] = max(1, min(40, int(cfg.get("drag_threshold", 5))))
    except Exception:
        cfg["drag_threshold"] = 5

    pool = cfg.get("click_reactions", DEFAULTS["click_reactions"])
    if not isinstance(pool, list):
        pool = DEFAULTS["click_reactions"]
    pool = [r for r in pool if r in REACTIONS]
    cfg["click_reactions"] = pool or list(DEFAULTS["click_reactions"])

    hm = cfg.get("hit_mode", "any")
    cfg["hit_mode"] = hm if hm in ("any", "opaque") else "any"

    # ---- 区域（新模型优先，兼容旧 click_zones）----
    regions = cfg.get("regions")
    if not isinstance(regions, list):
        regions = []
        old = cfg.get("click_zones", [])
        if isinstance(old, list):
            for z in old:
                if not isinstance(z, dict):
                    continue
                rect = z.get("rect")
                if not (isinstance(rect, (list, tuple)) and len(rect) == 4):
                    continue
                regions.append({
                    "name": z.get("name", "区域"),
                    "type": "rect",
                    "rect": rect,
                    "speech": z.get("speech", []),
                    "reaction": z.get("reaction", ""),
                    "no_drag": False,
                    "color": "",
                })
    clean = []
    for z in regions:
        if not isinstance(z, dict):
            continue
        typ = z.get("type", "rect")
        if typ not in ("rect", "paint"):
            typ = "rect"
        rect = z.get("rect")
        if not (isinstance(rect, (list, tuple)) and len(rect) == 4):
            continue
        try:
            r = [float(v) for v in rect]
        except Exception:
            continue
        r = [max(0.0, min(1.0, v)) for v in r]
        if typ == "rect" and (abs(r[2] - r[0]) < 0.005 or abs(r[3] - r[1]) < 0.005):
            continue
        sp = z.get("speech", [])
        if not isinstance(sp, list):
            sp = [str(sp)] if sp else []
        react = z.get("reaction", "")
        if react not in REACTIONS + ["", "__none__"]:
            react = ""
        color = str(z.get("color", ""))
        if not QColor(color).isValid():
            color = ""
        clean.append({
            "name": str(z.get("name", "区域")),
            "type": typ,
            "rect": r,
            "color": color if typ == "paint" else "",
            "speech": [str(s) for s in sp if str(s).strip()],
            "reaction": react,
            "no_drag": _to_bool(z.get("no_drag", False)),
        })
    cfg["regions"] = clean
    return cfg


def resource_path(rel):
    if hasattr(sys, "_MEIPASS"):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, rel)


CFG = load_config()
FRAME_COUNT = CFG["frame_count"]
PET_NAME = CFG["name"]
BASE_W, BASE_H = 994, 2165
MIN_HEIGHT, MAX_HEIGHT = 140, 1200
_SETTINGS_KEY = "pet_" + hashlib.md5(PET_NAME.encode("utf-8")).hexdigest()[:8]
ORG = "DesktopPetStudio"
APP = _SETTINGS_KEY


def frame_file(i):
    base = resource_path(os.path.join("assets", "frame_%04d" % i))
    for ext in (".webp", ".png"):
        p = base + ext
        if os.path.exists(p):
            return p
    return base + ".webp"


def mask_file():
    return resource_path(os.path.join("assets", "paint_mask.png"))


class Bubble(QLabel):
    def __init__(self):
        super().__init__(None)
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.Tool |
            Qt.WindowStaysOnTopHint | Qt.WindowTransparentForInput
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAlignment(Qt.AlignCenter)
        self.setWordWrap(True)
        fs = CFG["bubble_font_size"]
        self.stylesheet = (
            "QLabel{color:%s;background:%s;border:1px solid %s;"
            "border-radius:12px;padding:8px 12px;font:%dpx 'Microsoft YaHei';}"
            % (CFG["bubble_text"], CFG["bubble_bg"], CFG["bubble_border"], fs)
        )
        self.setStyleSheet(self.stylesheet)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.hide)
        self._owner = None
        self._font = QFont("Microsoft YaHei", fs)

    def say(self, text, owner):
        self._owner = owner
        self.setText(text)
        fm = QFontMetrics(self._font)
        w = min(300, max(120, fm.horizontalAdvance(text) + 34))
        self.setFixedWidth(w)
        self.adjustSize()
        self.place()
        self.show()
        self.raise_()
        self.timer.start(2600)

    def place(self):
        if not self._owner:
            return
        ow = self._owner
        x = ow.x() + (ow.width() - self.width()) // 2
        y = ow.y() - self.height() - 6
        if y < 4:
            y = ow.y() + ow.height() + 6
        screen = QGuiApplication.primaryScreen().availableGeometry()
        x = max(screen.left() + 4, min(x, screen.right() - self.width() - 4))
        self.move(x, y)


class Pet(QWidget):
    def __init__(self):
        super().__init__()
        self.settings = QSettings(ORG, APP)
        self.pet_height = int(self.settings.value("height", CFG["default_height"]))
        self.pet_height = max(MIN_HEIGHT, min(MAX_HEIGHT, self.pet_height))
        self.fps = int(self.settings.value("fps", CFG["default_fps"]))
        self.on_top = self.settings.value("ontop", True, type=bool)
        self.double_click_hide = self.settings.value("dblhide", CFG["double_click_hide"], type=bool)
        self.edge_hide = self.settings.value("edgehide", CFG["edge_hide"], type=bool)
        # v2 开关：运行时用户切换记住，首次取生成器配置
        self.hit_mode = self.settings.value("hitmode", CFG["hit_mode"])
        if self.hit_mode not in ("any", "opaque"):
            self.hit_mode = CFG["hit_mode"]
        self.region_drag_block = self.settings.value("rgdrag", CFG["region_drag_block"], type=bool)
        self.drag_threshold = int(self.settings.value("dragth", CFG["drag_threshold"]))

        self.frame = 0
        self.playing = True
        self.cache = {}
        self._alpha_cache = {}
        self.warm_i = 0
        self._drag_pos = None
        self._press_global = None
        self._press_zone_no_drag = False
        self._moved = False
        self.edge_side = None
        self._react_anim = None

        # 涂色掩膜（可选）
        self._mask = QImage()
        mp = mask_file()
        if os.path.exists(mp):
            loaded = QImage(mp)
            if not loaded.isNull():
                self._mask = loaded.convertToFormat(QImage.Format_ARGB32)

        flags = Qt.FramelessWindowHint | Qt.Tool
        if self.on_top:
            flags |= Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setMouseTracking(True)
        self.setWindowTitle(PET_NAME)

        first = QPixmap(frame_file(0))
        self.ratio = (first.width() / first.height()) if not first.isNull() else BASE_W / BASE_H
        self._apply_size()
        self._restore_pos()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(max(10, int(1000 / max(1, self.fps))))

        self.warm_timer = QTimer(self)
        self.warm_timer.timeout.connect(self._warm)
        self.warm_timer.start(12)

        self.edge_timer = QTimer(self)
        self.edge_timer.timeout.connect(self._check_edge)
        self.edge_timer.start(180)

        self.bubble = Bubble()
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_menu)
        self._setup_tray()

    # ---------- 尺寸 / 缓存 ----------
    def _apply_size(self):
        w = max(1, int(self.pet_height * self.ratio))
        self.resize(w, self.pet_height)
        self.cache.clear()
        self.warm_i = 0

    def pixmap(self, i):
        i %= FRAME_COUNT
        if i not in self.cache:
            src = QPixmap(frame_file(i))
            if not src.isNull():
                self.cache[i] = src.scaled(self.size(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            else:
                self.cache[i] = QPixmap()
        return self.cache[i]

    def alpha_image(self, i):
        i %= FRAME_COUNT
        if i not in self._alpha_cache:
            src = QImage(frame_file(i))
            img = src.convertToFormat(QImage.Format_ARGB32) if not src.isNull() else QImage()
            if len(self._alpha_cache) >= 24:
                self._alpha_cache.pop(next(iter(self._alpha_cache)))
            self._alpha_cache[i] = img
        return self._alpha_cache[i]

    def _warm(self):
        if self.warm_i >= FRAME_COUNT:
            self.warm_timer.stop()
            return
        self.pixmap(self.warm_i)
        self.warm_i += 1

    def _tick(self):
        if self.playing:
            self.frame = (self.frame + 1) % FRAME_COUNT
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        pm = self.pixmap(self.frame)
        if not pm.isNull():
            p.drawPixmap(self.rect(), pm)

    # ---------- 位置 ----------
    def _restore_pos(self):
        px = self.settings.value("pos_x", None)
        py = self.settings.value("pos_y", None)
        screen = QGuiApplication.primaryScreen().availableGeometry()
        if px is None or py is None:
            x = screen.right() - self.width() - 60
            y = screen.bottom() - self.height() - 40
        else:
            x, y = int(px), int(py)
        x = max(screen.left(), min(x, screen.right() - self.width()))
        y = max(screen.top(), min(y, screen.bottom() - self.height()))
        self.move(x, y)

    def _save_pos(self):
        if self.edge_side is None:
            self.settings.setValue("pos_x", self.x())
            self.settings.setValue("pos_y", self.y())

    # ---------- 鼠标交互 ----------
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag_pos = e.position().toPoint()
            self._press_global = e.globalPosition().toPoint()
            self._moved = False
            self._press_zone_no_drag = False
            if self.region_drag_block:
                z = self._zone_at(self._drag_pos)
                if z and z.get("no_drag"):
                    self._press_zone_no_drag = True
            self.bubble.hide()

    def mouseMoveEvent(self, e):
        if self._drag_pos is not None and (e.buttons() & Qt.LeftButton):
            gp = e.globalPosition().toPoint()
            if (gp - self._press_global).manhattanLength() > self.drag_threshold:
                self._moved = True
            if self._press_zone_no_drag:
                return
            self.move(gp - self._drag_pos)
            self.bubble.place()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            if not self._moved:
                self._on_click(e.position().toPoint())
            self._drag_pos = None
            self._press_global = None
            self._press_zone_no_drag = False
            self._maybe_edge_hide()

    def mouseDoubleClickEvent(self, e):
        if self.double_click_hide:
            self._toggle_visible()

    def wheelEvent(self, e):
        step = 20 if e.angleDelta().y() > 0 else -20
        newh = max(MIN_HEIGHT, min(MAX_HEIGHT, self.pet_height + step))
        if newh != self.pet_height:
            cx = self.x() + self.width() / 2
            by = self.y() + self.pet_height
            self.pet_height = newh
            self._apply_size()
            self.move(int(cx - self.width() / 2), int(by - self.pet_height))
            self.settings.setValue("height", self.pet_height)
            self.bubble.place()

    # ---------- 命中判定 ----------
    def _opaque_at(self, nx, ny):
        img = self.alpha_image(self.frame)
        if img.isNull():
            return True
        x = max(0, min(img.width() - 1, int(nx * max(0, img.width() - 1))))
        y = max(0, min(img.height() - 1, int(ny * max(0, img.height() - 1))))
        return img.pixelColor(x, y).alpha() > 40

    def _zone_at(self, pos):
        zones = CFG["regions"]
        if not zones:
            return None
        nx = pos.x() / max(1, self.width())
        ny = pos.y() / max(1, self.height())

        def area(z):
            x0, y0, x1, y1 = z["rect"]
            return abs(x1 - x0) * abs(y1 - y0)

        # paint 优先；同类按面积从小到大（小区域覆盖大区域）
        ordered = sorted(zones, key=lambda z: (0 if z["type"] == "paint" else 1, area(z)))
        for z in ordered:
            x0, y0, x1, y1 = z["rect"]
            if not (min(x0, x1) <= nx <= max(x0, x1) and min(y0, y1) <= ny <= max(y0, y1)):
                continue
            if z["type"] == "paint":
                if self._mask.isNull() or not z.get("color"):
                    continue
                mx = max(0, min(self._mask.width() - 1, int(nx * max(0, self._mask.width() - 1))))
                my = max(0, min(self._mask.height() - 1, int(ny * max(0, self._mask.height() - 1))))
                got = QColor(self._mask.pixelColor(mx, my))
                want = QColor(z["color"])
                if got.alpha() < 40 or (got.red(), got.green(), got.blue()) != (want.red(), want.green(), want.blue()):
                    continue
            if self.hit_mode == "opaque" and not self._opaque_at(nx, ny):
                continue
            return z
        return None

    def _on_click(self, pos):
        zone = self._zone_at(pos)
        if zone:
            lines = zone["speech"] or CFG["speech"]
            self.bubble.say(random.choice(lines), self)
            react = zone.get("reaction", "")
            if react == "__none__":
                return
            if react in REACTIONS:
                self._play_reaction(react)
            else:
                self._play_reaction(random.choice(CFG["click_reactions"]))
        else:
            self.bubble.say(random.choice(CFG["speech"]), self)
            self._play_reaction(random.choice(CFG["click_reactions"]))

    def _play_reaction(self, kind):
        origin = self.pos()
        if self._react_anim and self._react_anim.state() == QPropertyAnimation.Running:
            self._react_anim.stop()
        anim = QPropertyAnimation(self, b"pos")
        anim.setDuration(520)
        anim.setEasingCurve(QEasingCurve.OutElastic)
        if kind == "jump":
            anim.setKeyValueAt(0.0, origin); anim.setKeyValueAt(0.4, origin + QPoint(0, -34)); anim.setKeyValueAt(1.0, origin)
        elif kind == "blush":
            anim.setKeyValueAt(0.0, origin); anim.setKeyValueAt(0.3, origin + QPoint(-8, 0)); anim.setKeyValueAt(0.6, origin + QPoint(8, 0)); anim.setKeyValueAt(1.0, origin)
        elif kind == "spin":
            anim.setKeyValueAt(0.0, origin); anim.setKeyValueAt(0.5, origin + QPoint(-16, -6)); anim.setKeyValueAt(1.0, origin)
        elif kind == "wink":
            anim.setKeyValueAt(0.0, origin); anim.setKeyValueAt(0.25, origin + QPoint(0, 5)); anim.setKeyValueAt(0.5, origin + QPoint(0, -10)); anim.setKeyValueAt(1.0, origin)
        else:
            anim.setKeyValueAt(0.0, origin); anim.setKeyValueAt(0.5, origin + QPoint(0, -8)); anim.setKeyValueAt(1.0, origin)
        self._react_anim = anim
        anim.start()

    # ---------- 贴边自动收纳 ----------
    def _maybe_edge_hide(self):
        if not self.edge_hide:
            return
        screen = QGuiApplication.primaryScreen().availableGeometry()
        left_dist = abs(self.x() - screen.left())
        right_dist = abs((self.x() + self.width()) - screen.right())
        threshold = 70
        if left_dist < threshold and left_dist <= right_dist:
            self.edge_side = "left"
            self.move(screen.left() - self.width() + 14, self.y())
        elif right_dist < threshold:
            self.edge_side = "right"
            self.move(screen.right() - 14, self.y())

    def _check_edge(self):
        if not self.edge_hide or self.edge_side is None:
            return
        cp = QCursor.pos()
        screen = QGuiApplication.primaryScreen().availableGeometry()
        if self.edge_side == "left":
            near = abs(cp.x() - screen.left()) < 46 and self.y() <= cp.y() <= self.y() + self.pet_height
            if near:
                self.move(screen.left(), self.y()); self.edge_side = None
        else:
            near = abs(cp.x() - screen.right()) < 46 and self.y() <= cp.y() <= self.y() + self.pet_height
            if near:
                self.move(screen.right() - self.width(), self.y()); self.edge_side = None

    # ---------- 菜单 / 托盘 ----------
    def show_menu(self, pos):
        menu = QMenu(self)
        self._style_menu(menu)
        act = menu.addAction("暂停播放" if self.playing else "继续播放")
        act.triggered.connect(self.toggle_play)

        speed = menu.addMenu("播放速度")
        for label, fps in [("慢速 (12)", 12), ("正常 (24)", 24), ("快速 (36)", 36), ("极速 (60)", 60)]:
            a = speed.addAction(label); a.setCheckable(True); a.setChecked(self.fps == fps)
            a.triggered.connect(lambda _, f=fps: self.set_fps(f))

        base = CFG["default_height"]
        size = menu.addMenu("尺寸")
        for label, h in [("小 (%d)" % int(base * 0.67), int(base * 0.67)), ("中 (%d)" % base, base),
                         ("大 (%d)" % int(base * 1.4), int(base * 1.4))]:
            a = size.addAction(label); a.setCheckable(True); a.setChecked(self.pet_height == h)
            a.triggered.connect(lambda _, hh=h: self.set_height(hh))

        hit = menu.addMenu("命中判定")
        for label, mode in [("框内即命中", "any"), ("仅命中角色像素", "opaque")]:
            a = hit.addAction(label); a.setCheckable(True); a.setChecked(self.hit_mode == mode)
            a.triggered.connect(lambda _, m=mode: self.set_hit_mode(m))

        menu.addSeparator()
        a = menu.addAction("始终置顶"); a.setCheckable(True); a.setChecked(self.on_top); a.triggered.connect(self.toggle_ontop)
        a = menu.addAction("双击隐藏"); a.setCheckable(True); a.setChecked(self.double_click_hide); a.triggered.connect(self.toggle_double_hide)
        a = menu.addAction("贴边自动收纳"); a.setCheckable(True); a.setChecked(self.edge_hide); a.triggered.connect(self.toggle_edge)
        a = menu.addAction("区域禁止拖拽"); a.setCheckable(True); a.setChecked(self.region_drag_block); a.triggered.connect(self.toggle_region_drag)
        menu.addSeparator()
        a = menu.addAction("隐藏 / 显示（也可双击）"); a.triggered.connect(self._toggle_visible)
        a = menu.addAction("复位到右下角"); a.triggered.connect(self.reset_pos)
        a = menu.addAction("退出"); a.triggered.connect(self.quit_app)
        menu.exec(self.mapToGlobal(pos))

    def _style_menu(self, menu):
        menu.setStyleSheet(
            "QMenu{background:#20232b;color:#e8eaed;border:1px solid #3a3f4b;border-radius:8px;padding:5px;font:13px 'Microsoft YaHei';}"
            "QMenu::item{padding:6px 24px;border-radius:5px;}"
            "QMenu::item:selected{background:#3a3f4b;}"
            "QMenu::separator{height:1px;background:#3a3f4b;margin:4px 6px;}"
        )

    def _setup_tray(self):
        icon = QPixmap(frame_file(0)).scaled(64, 64, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.tray_icon = QIcon(icon)
        self.tray = QSystemTrayIcon(self.tray_icon, self)
        self.tray.setToolTip(PET_NAME + " - 右键菜单")
        tm = QMenu()
        self._style_menu(tm)
        a = tm.addAction("显示 / 隐藏"); a.triggered.connect(self._toggle_visible)
        a = tm.addAction("暂停 / 继续"); a.triggered.connect(self.toggle_play)
        tm.addSeparator()
        a = tm.addAction("退出"); a.triggered.connect(self.quit_app)
        self.tray.setContextMenu(tm)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.DoubleClick, QSystemTrayIcon.Trigger):
            self._toggle_visible()

    def _toggle_visible(self):
        if self.isVisible():
            self.bubble.hide(); self.hide()
        else:
            self.show(); self.raise_(); self.bubble.place()

    # ---------- 菜单动作 ----------
    def toggle_play(self):
        self.playing = not self.playing

    def set_fps(self, f):
        self.fps = f
        self.timer.setInterval(max(10, int(1000 / f)))
        self.settings.setValue("fps", f)

    def set_height(self, h):
        cx = self.x() + self.width() / 2
        by = self.y() + self.pet_height
        self.pet_height = h
        self._apply_size()
        self.move(int(cx - self.width() / 2), int(by - self.pet_height))
        self.settings.setValue("height", h)
        self.bubble.place()

    def set_hit_mode(self, m):
        self.hit_mode = m
        self.settings.setValue("hitmode", m)

    def toggle_ontop(self):
        self.on_top = not self.on_top
        self.setWindowFlags((Qt.FramelessWindowHint | Qt.Tool) | (Qt.WindowStaysOnTopHint if self.on_top else 0))
        self.show(); self.raise_()
        self.settings.setValue("ontop", self.on_top)

    def toggle_double_hide(self, checked=None):
        self.double_click_hide = (not self.double_click_hide) if checked is None else bool(checked)
        self.settings.setValue("dblhide", self.double_click_hide)

    def toggle_edge(self):
        self.edge_hide = not self.edge_hide
        if not self.edge_hide and self.edge_side:
            screen = QGuiApplication.primaryScreen().availableGeometry()
            self.move(screen.left() if self.edge_side == "left" else screen.right() - self.width(), self.y())
            self.edge_side = None
        self.settings.setValue("edgehide", self.edge_hide)

    def toggle_region_drag(self):
        self.region_drag_block = not self.region_drag_block
        self.settings.setValue("rgdrag", self.region_drag_block)

    def reset_pos(self):
        self.edge_side = None
        screen = QGuiApplication.primaryScreen().availableGeometry()
        self.move(screen.right() - self.width() - 60, screen.bottom() - self.height() - 40)
        self._save_pos(); self.bubble.place()

    def quit_app(self):
        self._save_pos(); self.settings.sync(); self.tray.hide()
        QApplication.quit()

    def closeEvent(self, e):
        self._save_pos(); self.settings.sync()
        super().closeEvent(e)


def main():
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    pet = Pet()
    pet.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
