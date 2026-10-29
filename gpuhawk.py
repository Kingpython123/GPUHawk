"""GPUHawk —— AutoDL / 晨涧云 GPU 空闲监控与抢卡桌面客户端（PySide6 + PyQt-Fluent-Widgets）。

运行：python gpuhawk.py
抢卡/查询逻辑复用 gpuhawk_core.py；token 存在 .env（exe 旁边），其余设置存在 QSettings。
"""

from __future__ import annotations

import math
import random
import sys
import time
from collections import deque
from datetime import datetime
from types import SimpleNamespace

import requests
from PySide6.QtCore import QEasingCurve, QPointF, QRectF, QSettings, Qt, QThread, QTimer, QUrl, QVariantAnimation, Signal
from PySide6.QtGui import (
    QColor, QConicalGradient, QDesktopServices, QFont, QIcon, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCompleter, QHBoxLayout, QHeaderView, QSystemTrayIcon,
    QTableWidgetItem, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    Action, BodyLabel, CaptionLabel, CardWidget, CheckBox, ComboBox, DoubleSpinBox, EditableComboBox,
    FlowLayout, FluentIcon as FIF, FluentWindow, GroupHeaderCardWidget, HeaderCardWidget, HyperlinkButton,
    IconWidget, IndeterminateProgressRing, InfoBar, InfoBarPosition, LineEdit, MessageBox, MessageBoxBase,
    NavigationItemPosition, PasswordLineEdit, PillPushButton, PlainTextEdit, PrimaryPushButton, PushButton,
    ScrollArea, SegmentedWidget, SpinBox, SubtitleLabel, SwitchButton, SystemTrayMenu, TableWidget, Theme, TitleLabel,
    isDarkTheme, setTheme, setThemeColor, themeColor,
)

import gpuhawk_core as core

ENV_FILE = core.ENV_FILE
APP_NAME = "GPUHawk"
THEMES = {"跟随系统": Theme.AUTO, "浅色": Theme.LIGHT, "深色": Theme.DARK}
IDLE_BG = QColor(34, 197, 94, 55)
GIB = 1024 ** 3
AUTODL, MORNAI = "AutoDL", "晨涧云"
# (显示名, 渠道, 输入框提示, 获取 key 的页面)
PUSH_OPTIONS = [
    ("不推送", "", "", ""),
    ("PushPlus（免费约 200 条/天）", "pushplus", "PushPlus token", "https://www.pushplus.plus/"),
    ("WxPusher 极简推送（免费）", "wxpusher", "SPT_ 开头的 SPT", "https://wxpusher.zjiecode.com/docs/"),
    ("Server酱（免费 5 条/天）", "serverchan", "SendKey（SCT 或 sctp 开头）", "https://sct.ftqq.com/"),
    ("企业微信群机器人", "wecom", "webhook 地址或 key", "https://developer.work.weixin.qq.com/document/path/91770"),
]


def save_token(token: str) -> None:
    lines = ENV_FILE.read_text(encoding="utf-8-sig").splitlines() if ENV_FILE.exists() else []
    lines = [ln for ln in lines if ln.partition("=")[0].strip() != "AUTODL_TOKEN"]
    lines.append(f"AUTODL_TOKEN={token}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_icon() -> QIcon:
    pix = QPixmap(256, 256)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#2563eb"))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(16, 16, 224, 224, 56, 56)
    p.setPen(QColor("white"))
    p.setFont(QFont("Segoe UI", 64, QFont.Bold))
    p.drawText(pix.rect(), Qt.AlignCenter, "GPU")
    p.end()
    return QIcon(pix)


def fmt_gb(n: int) -> str:
    return f"{n / GIB:.0f} GB" if n else "-"


def machine_row(m: dict) -> list[str]:
    """机器明细表的一行。字段来自 /user/machine/list 实测：价格单位 1/1000 元，内存单位字节。"""
    base = m.get("machine_base_info") or {}
    total = m.get("gpu_number") or "?"
    cpu = m.get("cpu_per_gpu")
    return [
        m.get("machine_alias") or m.get("machine_id", ""),
        f"{m.get('gpu_idle_num') or 0}/{total}",
        f"¥{(m.get('payg_price') or 0) / 1000:.2f}",
        f"{cpu} 核" if cpu else "-",
        fmt_gb(m.get("mem_per_gpu") or 0),
        m.get("highest_cuda_version") or "-",
        (base.get("cpu_name") or "-").replace("(R)", "").replace(" CPU", ""),
    ]


# ---------------------------------------------------------------- 后台线程

class PushWorker(QThread):
    done = Signal(bool, str)

    def __init__(self, channel: str, key: str, title: str, content: str):
        super().__init__()
        self.args = (channel, key, title, content)

    def run(self):
        self.done.emit(*core.push(*self.args))


class MetaWorker(QThread):
    """拉 AutoDL 型号（含显存）、地区分组和晨涧云型号，都是公开接口。晨涧云失败不影响 AutoDL。"""
    done = Signal(dict, dict, list)
    failed = Signal(str)

    def run(self):
        try:
            api = core.AutoDL("")
            catalog, groups = api.gpu_catalog(), api.region_groups()
        except Exception as exc:  # noqa: BLE001 - 任何失败都报给界面
            self.failed.emit(str(exc))
            return
        try:
            mornai = core.MornAI().gpu_counts("cloud")
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"晨涧云型号加载失败：{exc}")
            mornai = []
        self.done.emit(catalog, groups, mornai)


class TokenWorker(QThread):
    ok = Signal(dict)
    failed = Signal(str)

    def __init__(self, token: str, sub_user: bool):
        super().__init__()
        self.token, self.sub_user = token, sub_user

    def run(self):
        try:
            self.ok.emit(core.AutoDL(self.token, self.sub_user).user())
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


def emit_mornai(signal) -> None:
    """晨涧云云主机、云容器各一张表，按 (平台, 类型, 表) 发出去。"""
    api = core.MornAI()
    for kind, label in core.MORNAI_KINDS.items():
        signal.emit(MORNAI, label, api.gpu_table(kind))


class InventoryWorker(QThread):
    """手动刷新：AutoDL 所有地区（有 token 时）+ 晨涧云，全部型号库存。"""
    table = Signal(str, str, dict)
    failed = Signal(str)

    def __init__(self, token: str, sub_user: bool, groups: dict):
        super().__init__()
        self.token, self.sub_user, self.groups = token, sub_user, groups

    def run(self):
        if self.token:
            api = core.AutoDL(self.token, self.sub_user)
            try:
                for name, signs in self.groups.values():
                    self.table.emit(AUTODL, name, api.gpu_table(signs))
            except Exception as exc:  # noqa: BLE001
                self.failed.emit(f"AutoDL：{exc}")
        try:
            emit_mornai(self.table)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"晨涧云：{exc}")


class MachineWorker(QThread):
    """某地区某型号的机器明细。"""
    done = Signal(list)
    failed = Signal(str)

    def __init__(self, token: str, sub_user: bool, signs: list, gpu: str):
        super().__init__()
        self.token, self.sub_user, self.signs, self.gpu = token, sub_user, signs, gpu

    def run(self):
        try:
            self.done.emit(core.AutoDL(self.token, self.sub_user).machines(self.signs, self.gpu, 0))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class GrabWorker(QThread):
    """cfg.gpu 为空时不查 AutoDL；cfg.mornai_gpu 为空时不查晨涧云。自动开机只针对 AutoDL。"""
    log = Signal(str)
    table = Signal(str, str, dict)
    round_done = Signal(int)
    succeeded = Signal(dict)
    auth_failed = Signal(str)

    def __init__(self, token: str, cfg: SimpleNamespace, groups: dict):
        super().__init__()
        self.token, self.cfg, self.groups = token, cfg, groups
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        api = core.AutoDL(self.token, sub_user=self.cfg.sub_user)
        rounds = 0
        while not self._stop:
            rounds += 1
            result = None
            if self.cfg.gpu:
                try:
                    result = core.try_grab(api, self.cfg, self.groups, log=self.log.emit,
                                           on_table=lambda r, t: self.table.emit(AUTODL, r, t))
                except core.AuthError as exc:
                    self.auth_failed.emit(str(exc))
                    return
                except (requests.RequestException, RuntimeError, ValueError) as exc:
                    self.log.emit(f"AutoDL 本轮出错：{exc}")
            if self.cfg.mornai_gpu and not result:
                try:
                    emit_mornai(self.table)
                except (requests.RequestException, RuntimeError, ValueError) as exc:
                    self.log.emit(f"晨涧云本轮出错：{exc}")
            self.round_done.emit(rounds)
            if result:
                self.succeeded.emit(result)
                return
            end = time.monotonic() + self.cfg.interval * random.uniform(0.8, 1.3)
            while not self._stop and time.monotonic() < end:
                time.sleep(0.2)


# ---------------------------------------------------------------- 小组件

class StatCard(CardWidget):
    """顶部数据卡片：图标 + 标题 + 数值。"""

    def __init__(self, icon, title: str, parent=None):
        super().__init__(parent)
        self.setFixedHeight(78)
        iw = IconWidget(icon, self)
        iw.setFixedSize(26, 26)
        cap = CaptionLabel(title, self)
        cap.setTextColor(QColor(96, 96, 96), QColor(200, 200, 200))
        self.value = SubtitleLabel("-", self)
        col = QVBoxLayout()
        col.setSpacing(0)
        col.addWidget(cap)
        col.addWidget(self.value)
        row = QHBoxLayout(self)
        row.setContentsMargins(18, 10, 18, 10)
        row.setSpacing(14)
        row.addWidget(iw)
        row.addLayout(col)
        row.addStretch()

        self._num = 0
        self._anim: QVariantAnimation | None = None

    def set(self, text: str):
        if self._anim:
            self._anim.stop()
        self.value.setText(text)

    def set_number(self, n: int, fmt: str = "{}"):
        """数字滚动到新值。"""
        if n == self._num and self.value.text() == fmt.format(n):
            return
        if self._anim:
            self._anim.stop()
        anim = QVariantAnimation(self, startValue=self._num, endValue=n, duration=500)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.valueChanged.connect(lambda v: self.value.setText(fmt.format(int(v))))
        self.value.setText(fmt.format(self._num))  # 起止相同时动画不发信号，先写一次
        anim.start()
        self._anim, self._num = anim, n


class TrendChart(QWidget):
    """目标型号空闲卡数的折线面积图，每轮采样一个点。"""

    def __init__(self, parent=None, capacity: int = 120):
        super().__init__(parent)
        self.points: deque[tuple[datetime, int]] = deque(maxlen=capacity)
        self.setMinimumHeight(140)

    def add(self, value: int):
        self.points.append((datetime.now(), value))
        self.update()

    def clear(self):
        self.points.clear()
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        dark = isDarkTheme()
        sub = QColor(255, 255, 255, 150) if dark else QColor(0, 0, 0, 120)
        grid = QColor(255, 255, 255, 28) if dark else QColor(0, 0, 0, 20)
        r = QRectF(self.rect()).adjusted(6, 22, -6, -20)
        p.setPen(QPen(grid, 1, Qt.DashLine))
        for i in range(4):
            y = r.top() + r.height() * i / 3
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        p.setFont(QFont("Microsoft YaHei UI", 8))
        if len(self.points) < 2:
            p.setPen(sub)
            p.drawText(r, Qt.AlignCenter, "开始后每轮记录一次目标型号的空闲卡数")
            return

        vals = [v for _, v in self.points]
        vmax = max(max(vals), 1)
        n = len(vals)
        pts = [QPointF(r.left() + r.width() * i / (n - 1), r.bottom() - r.height() * v / vmax)
               for i, v in enumerate(vals)]
        line = QPainterPath(pts[0])
        for a, b in zip(pts, pts[1:]):
            mid = (a.x() + b.x()) / 2
            line.cubicTo(QPointF(mid, a.y()), QPointF(mid, b.y()), b)
        area = QPainterPath(line)
        area.lineTo(r.right(), r.bottom())
        area.lineTo(r.left(), r.bottom())
        area.closeSubpath()

        c = themeColor()
        grad = QLinearGradient(0, r.top(), 0, r.bottom())
        top, bottom = QColor(c), QColor(c)
        top.setAlpha(110)
        bottom.setAlpha(0)
        grad.setColorAt(0, top)
        grad.setColorAt(1, bottom)
        p.fillPath(area, grad)
        p.setPen(QPen(c, 2))
        p.drawPath(line)

        last = pts[-1]
        halo = QColor(c)
        halo.setAlpha(60)
        p.setPen(Qt.NoPen)
        p.setBrush(halo)
        p.drawEllipse(last, 8, 8)
        p.setBrush(c)
        p.drawEllipse(last, 4, 4)

        p.setPen(sub)
        p.drawText(QRectF(r.left(), 0, r.width(), 18), Qt.AlignLeft | Qt.AlignVCenter, f"峰值 {vmax} 张")
        p.drawText(QRectF(r.left(), 0, r.width(), 18), Qt.AlignRight | Qt.AlignVCenter, f"当前 {vals[-1]} 张")
        bottom_rect = QRectF(r.left(), r.bottom() + 4, r.width(), 16)
        p.drawText(bottom_rect, Qt.AlignLeft | Qt.AlignVCenter, f"{self.points[0][0]:%H:%M:%S}")
        p.drawText(bottom_rect, Qt.AlignRight | Qt.AlignVCenter, f"{self.points[-1][0]:%H:%M:%S}")


class Radar(QWidget):
    """标题栏旁的雷达：运行时扫描，发现空闲卡时冒光点。"""

    def __init__(self, parent=None, size: int = 40):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.angle = 0.0
        self.active = False
        self.blips: list[list[float]] = []  # [角度, 半径比例, 剩余亮度]
        self.timer = QTimer(self, interval=33, timeout=self._tick)  # 30fps 足够，60fps 白占一倍 CPU

    def start(self):
        self.active = True
        self.timer.start()

    def stop(self):
        self.active = False
        self.blips.clear()
        self.timer.stop()
        self.update()

    def ping(self):
        if self.active:
            self.blips.append([self.angle - random.uniform(10, 40), random.uniform(0.35, 0.85), 1.0])

    def _tick(self):
        self.angle = (self.angle + 8) % 360
        for b in self.blips:
            b[2] -= 0.024
        self.blips = [b for b in self.blips if b[2] > 0]
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = themeColor()
        center = QPointF(self.width() / 2, self.height() / 2)
        rad = min(self.width(), self.height()) / 2 - 1.5
        ring = QColor(c)
        ring.setAlpha(150 if self.active else 70)
        p.setPen(QPen(ring, 1.2))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(center, rad, rad)
        ring.setAlpha(ring.alpha() // 2)
        p.setPen(QPen(ring, 1))
        p.drawEllipse(center, rad * 0.55, rad * 0.55)
        p.drawLine(QPointF(center.x() - rad, center.y()), QPointF(center.x() + rad, center.y()))
        p.drawLine(QPointF(center.x(), center.y() - rad), QPointF(center.x(), center.y() + rad))
        if not self.active:
            return
        sweep = QConicalGradient(center, -self.angle)
        head, tail = QColor(c), QColor(c)
        head.setAlpha(170)
        tail.setAlpha(0)
        sweep.setColorAt(0, head)
        sweep.setColorAt(0.2, tail)
        sweep.setColorAt(1, tail)
        p.setPen(Qt.NoPen)
        p.setBrush(sweep)
        p.drawEllipse(center, rad, rad)
        for ang, dist, life in self.blips:
            a = math.radians(-ang)
            pos = QPointF(center.x() + math.cos(a) * rad * dist, center.y() + math.sin(a) * rad * dist)
            dot = QColor(34, 197, 94, int(255 * life))
            p.setBrush(dot)
            p.drawEllipse(pos, 2.5 + 2 * (1 - life), 2.5 + 2 * (1 - life))


class Confetti(QWidget):
    """抢卡成功时满窗口撒彩带。"""
    COLORS = ["#2563eb", "#22c55e", "#f59e0b", "#ef4444", "#a855f7", "#06b6d4", "#ec4899"]
    FRAMES = 230

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.parts: list[list] = []
        self.frame = 0
        self.timer = QTimer(self, interval=16, timeout=self._tick)
        self.hide()

    def burst(self):
        w = self.parent().width()
        self.setGeometry(self.parent().rect())
        self.parts = [[random.uniform(0, w), random.uniform(-260, -10), random.uniform(-2.2, 2.2),
                       random.uniform(2, 5.5), random.uniform(0, 360), random.uniform(-8, 8),
                       QColor(random.choice(self.COLORS)), random.uniform(6, 11), random.uniform(3, 6)]
                      for _ in range(180)]
        self.frame = 0
        self.raise_()
        self.show()
        self.timer.start()

    def _tick(self):
        self.frame += 1
        for q in self.parts:
            q[0] += q[2] + math.sin((self.frame + q[4]) / 12) * 0.6
            q[1] += q[3]
            q[3] += 0.05
            q[4] += q[5]
        if self.frame >= self.FRAMES:
            self.timer.stop()
            self.hide()
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        fade = min(1.0, (self.FRAMES - self.frame) / 60)
        p.setPen(Qt.NoPen)
        for x, y, _vx, _vy, rot, _vr, color, w, h in self.parts:
            col = QColor(color)
            col.setAlphaF(fade)
            p.save()
            p.translate(x, y)
            p.rotate(rot)
            p.setBrush(col)
            p.drawRoundedRect(QRectF(-w / 2, -h / 2, w, h), 1.5, 1.5)
            p.restore()


class MachineDialog(MessageBoxBase):
    """双击库存行弹出的机器明细。"""

    def __init__(self, title: str, worker: MachineWorker, parent=None):
        super().__init__(parent)
        head = QHBoxLayout()
        head.addWidget(SubtitleLabel(title, self))
        head.addStretch()
        self.ring = IndeterminateProgressRing(self)
        self.ring.setFixedSize(24, 24)
        self.ring.setStrokeWidth(3)
        head.addWidget(self.ring)
        self.viewLayout.addLayout(head)

        self.table = TableWidget(self)
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(["机器", "空闲/总卡", "每卡单价(元/时)", "每卡 CPU", "每卡内存", "最高 CUDA", "CPU 型号"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setBorderVisible(True)
        self.table.setBorderRadius(8)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMinimumHeight(320)
        self.viewLayout.addWidget(self.table)
        self.note = CaptionLabel("按单价从低到高排列，绿色为有空闲卡的机器", self)
        self.note.setTextColor(QColor(96, 96, 96), QColor(200, 200, 200))
        self.viewLayout.addWidget(self.note)

        self.widget.setMinimumWidth(900)
        self.yesButton.setText("关闭")
        self.cancelButton.hide()
        self.worker = worker
        worker.done.connect(self._fill)
        worker.failed.connect(self._fail)

    def _fill(self, machines: list):
        self.ring.hide()
        self.table.setRowCount(len(machines))
        for i, m in enumerate(machines):
            idle = m.get("gpu_idle_num") or 0
            for j, v in enumerate(machine_row(m)):
                item = QTableWidgetItem(str(v))
                if 0 < j < 6:
                    item.setTextAlignment(Qt.AlignCenter)
                if idle > 0:
                    item.setBackground(IDLE_BG)
                self.table.setItem(i, j, item)
        if not machines:
            self.note.setText("该地区暂时没有这个型号的机器")

    def _fail(self, err: str):
        self.ring.hide()
        self.note.setText(f"查询失败：{err}")


def section(card, title: str):
    card.setTitle(title)
    card.setBorderRadius(8)
    return card


def scroll_page(name: str, body: QWidget) -> ScrollArea:
    """页面套一层透明滚动区，小屏幕放不下时可以滚动。"""
    area = ScrollArea()
    area.setObjectName(name)
    area.setWidgetResizable(True)
    area.setWidget(body)
    body.setObjectName(f"{name}Body")
    area.setStyleSheet(f"QScrollArea{{background: transparent; border: none}} #{name}Body{{background: transparent}}")
    return area


# ---------------------------------------------------------------- 主窗口

class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("autodl-grab", "client")
        setTheme(THEMES.get(self.settings.value("theme", "跟随系统"), Theme.AUTO))
        setThemeColor("#2563eb")
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(make_icon())
        avail = QApplication.primaryScreen().availableGeometry()
        self.setMinimumSize(980, 600)
        self.resize(min(1280, avail.width() - 40), min(860, avail.height() - 40))
        self.move(avail.center() - self.rect().center())

        self.token = core.load_token()
        self.user_name = ""
        self.groups: dict = {}
        self.catalog: dict[str, int] = {}
        self.mornai: dict[str, dict] = {}  # 晨涧云 型号名 → {shortName, gpuMemory(GB)}
        # (平台, 地区/类型, 型号) → (空闲, 总数, 更新时间)；晨涧云没有总数，记 0
        self.inventory: dict[tuple[str, str, str], tuple[int, int, str]] = {}
        self.prev_idle: dict[tuple[str, str], int] = {}
        self.region_btns: list[PillPushButton] = []
        self._rows: list[tuple] = []  # 当前表格每行对应的 (平台, 地区, 型号, ...)，双击时用
        self.grab_worker: GrabWorker | None = None
        self._workers: list[QThread] = []
        self._quitting = False

        self.grab_page = self._build_grab_page()
        self.settings_page = self._build_settings_page()
        self.addSubInterface(self.grab_page, FIF.SPEED_HIGH, "抢卡")
        self.addSubInterface(self.settings_page, FIF.SETTING, "设置", NavigationItemPosition.BOTTOM)
        self.confetti = Confetti(self)
        self._render_timer = QTimer(self, singleShot=True, interval=150, timeout=self._render_table)
        self._build_tray()
        self._load_settings()
        self._load_meta()
        if self.token:
            self.token_edit.setText(self.token)
            self._check_token()
        else:
            self.stat_acct.set("未设置")

    # ---------------- 抢卡页

    def _build_grab_page(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(28, 16, 28, 20)
        root.setSpacing(14)

        # 标题 + 模式 + 开始
        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(0)
        titles.addWidget(TitleLabel("抢卡", page))
        self.status_label = CaptionLabel("待机中 · 先在「设置」里填 token", page)
        self.status_label.setTextColor(QColor(96, 96, 96), QColor(200, 200, 200))
        titles.addWidget(self.status_label)
        head.addLayout(titles)
        head.addStretch()
        self.radar = Radar(page)
        self.mode_seg = SegmentedWidget(page)
        self.mode_seg.addItem("monitor", "只监控提醒")
        self.mode_seg.addItem("buy", "有卡自动开机")
        self.start_btn = PrimaryPushButton(FIF.PLAY, "开始", page)
        self.start_btn.setFixedSize(128, 36)
        self.start_btn.clicked.connect(self._toggle)
        head.addWidget(self.radar, 0, Qt.AlignVCenter)
        head.addSpacing(14)
        # 不加对齐会被标题行拉高，选中背景只盖住上半截
        head.addWidget(self.mode_seg, 0, Qt.AlignVCenter)
        head.addSpacing(12)
        head.addWidget(self.start_btn)
        root.addLayout(head)

        # 数据卡片
        stats = QHBoxLayout()
        stats.setSpacing(12)
        self.stat_state = StatCard(FIF.ROBOT, "状态", page)
        self.stat_round = StatCard(FIF.SYNC, "轮询次数", page)
        self.stat_idle = StatCard(FIF.IOT, "目标型号空闲", page)
        self.stat_acct = StatCard(FIF.PEOPLE, "账号", page)
        self.stat_state.set("待机")
        self.stat_round.set("0")
        for c in (self.stat_state, self.stat_round, self.stat_idle, self.stat_acct):
            stats.addWidget(c)
        root.addLayout(stats)

        body = QHBoxLayout()
        body.setSpacing(14)

        # 左：目标 + 地区
        left = QWidget(page)
        left.setFixedWidth(430)
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(14)
        self.target_card = section(GroupHeaderCardWidget(left), "抢卡目标")
        self.gpu_combo = EditableComboBox()
        self.gpu_combo.setFixedWidth(190)
        self.gpu_combo.setPlaceholderText("留空 = 不监控")
        self.gpu_combo.currentTextChanged.connect(lambda _: self._render_table())
        self.mornai_combo = ComboBox()
        self.mornai_combo.setFixedWidth(190)
        self.mornai_combo.addItem("不监控", userData="")
        self.mornai_combo.currentIndexChanged.connect(lambda _: self._render_table())
        self.num_spin = SpinBox()
        self.num_spin.setRange(1, 8)
        self.num_spin.setFixedWidth(170)
        self.price_spin = DoubleSpinBox()
        self.price_spin.setRange(0, 200)
        self.price_spin.setSingleStep(0.1)
        self.price_spin.setDecimals(2)
        self.price_spin.setSuffix(" 元/时")
        self.price_spin.setSpecialValueText("不限")
        self.price_spin.setFixedWidth(170)
        self.interval_spin = SpinBox()
        self.interval_spin.setRange(5, 600)
        self.interval_spin.setSuffix(" 秒")
        self.interval_spin.setFixedWidth(170)
        self.target_card.addGroup(FIF.IOT, "AutoDL 型号", "下拉或输入搜索，可自动开机", self.gpu_combo)
        self.target_card.addGroup(FIF.CLOUD, "晨涧云型号", "免登录，只监控提醒", self.mornai_combo)
        self.target_card.addGroup(FIF.TILES, "卡数", "单个实例的 GPU 数", self.num_spin)
        self.target_card.addGroup(FIF.SHOPPING_CART, "价格上限", "每卡每小时，0 为不限", self.price_spin)
        self.target_card.addGroup(FIF.HISTORY, "轮询间隔", "实际间隔带随机抖动", self.interval_spin)
        ll.addWidget(self.target_card)

        self.region_card = section(HeaderCardWidget(left), "AutoDL 地区")
        hint = CaptionLabel("不选 = 全部地区", self.region_card)
        hint.setTextColor(QColor(96, 96, 96), QColor(200, 200, 200))
        self.region_card.headerLayout.addStretch(1)
        self.region_card.headerLayout.addWidget(hint)
        box = QWidget(self.region_card)
        self.region_flow = FlowLayout(box, needAni=False)
        self.region_flow.setContentsMargins(0, 0, 0, 0)
        self.region_flow.setHorizontalSpacing(8)
        self.region_flow.setVerticalSpacing(8)
        self.region_card.viewLayout.setContentsMargins(20, 14, 20, 18)
        self.region_card.viewLayout.addWidget(box)
        ll.addWidget(self.region_card)
        ll.addStretch()
        body.addWidget(left)

        # 右：库存 + 日志
        right = QVBoxLayout()
        right.setSpacing(14)
        inv = section(HeaderCardWidget(page), "实时库存")
        self.only_target_cb = CheckBox("只看目标型号")
        self.only_target_cb.setChecked(True)
        self.only_target_cb.toggled.connect(lambda _: self._render_table())
        self.only_idle_cb = CheckBox("只看有空闲")
        self.only_idle_cb.toggled.connect(lambda _: self._render_table())
        refresh = PushButton(FIF.SYNC, "刷新")
        refresh.clicked.connect(self._refresh_inventory)
        inv.headerLayout.addStretch(1)
        inv.headerLayout.addWidget(self.only_target_cb)
        inv.headerLayout.addSpacing(12)
        inv.headerLayout.addWidget(self.only_idle_cb)
        inv.headerLayout.addSpacing(12)
        inv.headerLayout.addWidget(refresh)
        inv_box = QWidget(inv)
        il = QVBoxLayout(inv_box)
        il.setContentsMargins(0, 0, 0, 0)
        il.setSpacing(8)
        self.table = TableWidget(inv_box)
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(["平台", "地区", "型号", "显存", "空闲", "总数", "更新时间"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setBorderVisible(True)
        self.table.setBorderRadius(8)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Stretch)
        for col in (0, 1, 2):
            hh.setSectionResizeMode(col, QHeaderView.Interactive)  # 宽度在 _render_table 里一次性算
        self.table.cellDoubleClicked.connect(self._open_machines)
        tip = CaptionLabel("双击 AutoDL 行看每台机器的 CPU、内存、单价；双击晨涧云行打开它的租用页", inv_box)
        tip.setTextColor(QColor(96, 96, 96), QColor(200, 200, 200))
        il.addWidget(self.table)
        il.addWidget(tip)
        inv.viewLayout.setContentsMargins(16, 12, 16, 12)
        inv.viewLayout.addWidget(inv_box)
        right.addWidget(inv, 3)

        log_card = section(HeaderCardWidget(page), "运行日志")
        clear = PushButton(FIF.DELETE, "清空")
        log_card.headerLayout.addStretch(1)
        log_card.headerLayout.addWidget(clear)
        self.log_view = PlainTextEdit(log_card)
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setFont(QFont("Consolas", 9))
        clear.clicked.connect(self.log_view.clear)
        log_card.viewLayout.setContentsMargins(16, 12, 16, 12)
        log_card.viewLayout.addWidget(self.log_view)

        trend_card = section(HeaderCardWidget(page), "空闲趋势")
        trend_hint = CaptionLabel("目标型号 · 每轮采样", trend_card)
        trend_hint.setTextColor(QColor(96, 96, 96), QColor(200, 200, 200))
        trend_card.headerLayout.addStretch(1)
        trend_card.headerLayout.addWidget(trend_hint)
        self.trend = TrendChart(trend_card)
        trend_card.viewLayout.setContentsMargins(16, 8, 16, 12)
        trend_card.viewLayout.addWidget(self.trend)

        bottom = QHBoxLayout()
        bottom.setSpacing(14)
        bottom.addWidget(trend_card, 1)
        bottom.addWidget(log_card, 1)
        right.addLayout(bottom, 2)
        body.addLayout(right, 1)
        root.addLayout(body, 1)
        self.table.setMinimumHeight(180)
        self.log_view.setMinimumHeight(110)
        return scroll_page("grabPage", page)

    # ---------------- 设置页

    def _build_settings_page(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(28, 16, 28, 20)
        root.setSpacing(14)
        root.addWidget(TitleLabel("设置", page))

        acct = section(GroupHeaderCardWidget(page), "账号")
        self.token_edit = PasswordLineEdit()
        self.token_edit.setPlaceholderText("粘贴 authorization（eyJ 开头）")
        self.token_edit.setFixedWidth(380)
        save_btn = PrimaryPushButton("保存并验证")
        save_btn.clicked.connect(self._save_token)
        tw = QWidget()
        th = QHBoxLayout(tw)
        th.setContentsMargins(0, 0, 0, 0)
        th.addWidget(self.token_edit)
        th.addWidget(save_btn)
        acct.addGroup(FIF.CERTIFICATE, "登录 Token",
                      "浏览器登录 autodl.com → F12 → 网络 → 任意 /api/v1/ 请求 → 请求头 authorization", tw)
        self.token_status = BodyLabel("未设置")
        acct.addGroup(FIF.PEOPLE, "登录状态", "token 等同账号密码，不要发给别人", self.token_status)
        self.sub_user_sw = SwitchButton()
        self.sub_user_sw.setOnText("是")
        self.sub_user_sw.setOffText("否")
        acct.addGroup(FIF.PEOPLE, "子账号", "用的是子账号 token 时打开", self.sub_user_sw)
        root.addWidget(acct)

        launch = section(GroupHeaderCardWidget(page), "开机参数")
        self.image_edit = LineEdit()
        self.image_edit.setFixedWidth(460)
        self.private_edit = LineEdit()
        self.private_edit.setPlaceholderText("如 image-4933eddca6，填了优先使用")
        self.private_edit.setFixedWidth(460)
        launch.addGroup(FIF.CLOUD, "公共镜像", "AutoDL 镜像地址", self.image_edit)
        launch.addGroup(FIF.PHOTO, "私有镜像", "我的镜像里的 UUID", self.private_edit)
        root.addWidget(launch)

        notice = section(GroupHeaderCardWidget(page), "微信提醒")
        self.push_combo = ComboBox()
        self.push_combo.addItems([o[0] for o in PUSH_OPTIONS])
        self.push_combo.setFixedWidth(260)
        self.push_link = HyperlinkButton("", "获取 key")
        self.push_combo.currentIndexChanged.connect(self._on_push_channel)
        pw = QWidget()
        ph = QHBoxLayout(pw)
        ph.setContentsMargins(0, 0, 0, 0)
        ph.addWidget(self.push_link)
        ph.addWidget(self.push_combo)
        notice.addGroup(FIF.MAIL, "推送渠道", "有卡（只监控模式）、抢到卡、token 失效时推送到微信", pw)
        self.push_key_edit = PasswordLineEdit()
        self.push_key_edit.setFixedWidth(320)
        self.push_test_btn = PushButton(FIF.SEND, "发送测试")
        self.push_test_btn.clicked.connect(self._test_push)
        kw = QWidget()
        kh = QHBoxLayout(kw)
        kh.setContentsMargins(0, 0, 0, 0)
        kh.addWidget(self.push_key_edit)
        kh.addWidget(self.push_test_btn)
        notice.addGroup(FIF.CERTIFICATE, "推送 Key", "在对应平台微信扫码登录后获取", kw)
        root.addWidget(notice)

        other = section(GroupHeaderCardWidget(page), "外观")
        self.theme_combo = ComboBox()
        self.theme_combo.addItems(list(THEMES))
        self.theme_combo.setFixedWidth(160)
        self.theme_combo.currentTextChanged.connect(self._change_theme)
        other.addGroup(FIF.BRUSH, "主题", "浅色 / 深色 / 跟随系统", self.theme_combo)
        root.addWidget(other)
        root.addStretch()
        return scroll_page("settingsPage", page)

    def _build_tray(self):
        self.tray = QSystemTrayIcon(make_icon(), self)
        self.tray.setToolTip(APP_NAME)
        menu = SystemTrayMenu(parent=self)
        menu.addActions([
            Action(FIF.HOME, "显示窗口", triggered=self._show_window),
            Action(FIF.CLOSE, "退出", triggered=self._quit),
        ])
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda r: self._show_window() if r == QSystemTrayIcon.Trigger else None)
        self.tray.show()

    # ---------------- 设置持久化

    def _load_settings(self):
        s = self.settings
        self._saved_gpu = s.value("gpu", "RTX 4090")
        self.gpu_combo.setText(self._saved_gpu)
        self._saved_mornai = s.value("mornai_gpu", "")
        self.num_spin.setValue(int(s.value("num", 1)))
        self.price_spin.setValue(float(s.value("max_price", 0)))
        self.interval_spin.setValue(int(s.value("interval", 15)))
        self.image_edit.setText(s.value("image", core.DEFAULT_IMAGE))
        self.private_edit.setText(s.value("private_image", ""))
        # 旧版只有 Server酱 SendKey，迁移成新的渠道设置
        if s.value("sckey") and not s.value("push_channel"):
            s.setValue("push_channel", "serverchan")
            s.setValue("push_key/serverchan", s.value("sckey"))
            s.remove("sckey")
        channel = s.value("push_channel", "")
        self._push_channel = None
        self.push_combo.setCurrentIndex(next((i for i, o in enumerate(PUSH_OPTIONS) if o[1] == channel), 0))
        self._on_push_channel(self.push_combo.currentIndex())
        self.sub_user_sw.setChecked(s.value("sub_user", "false") == "true")
        self.mode_seg.setCurrentItem("buy" if s.value("mode", "monitor") == "buy" else "monitor")
        self.theme_combo.setCurrentText(s.value("theme", "跟随系统"))
        regions = s.value("regions", []) or []
        self._saved_regions = {regions} if isinstance(regions, str) else set(regions)

    def _save_settings(self):
        s = self.settings
        s.setValue("gpu", self.gpu_combo.text().strip())
        if self.mornai:  # 型号列表没加载出来时别把保存的选择冲掉
            s.setValue("mornai_gpu", self._mornai_target())
        s.setValue("num", self.num_spin.value())
        s.setValue("max_price", self.price_spin.value())
        s.setValue("interval", self.interval_spin.value())
        s.setValue("image", self.image_edit.text().strip())
        s.setValue("private_image", self.private_edit.text().strip())
        s.setValue("push_channel", self._push_channel or "")
        if self._push_channel:
            s.setValue(f"push_key/{self._push_channel}", self.push_key_edit.text().strip())
        s.setValue("sub_user", "true" if self.sub_user_sw.isChecked() else "false")
        s.setValue("mode", self.mode_seg.currentRouteKey())
        s.setValue("regions", self._checked_regions())

    def _checked_regions(self) -> list[str]:
        return [b.text() for b in self.region_btns if b.isChecked()]

    def _change_theme(self, name: str):
        setTheme(THEMES.get(name, Theme.AUTO))
        self.settings.setValue("theme", name)

    def _on_push_channel(self, index: int):
        """切换推送渠道：先存下旧渠道的 key，再载入新渠道的。"""
        if self._push_channel:
            self.settings.setValue(f"push_key/{self._push_channel}", self.push_key_edit.text().strip())
        _, channel, hint, url = PUSH_OPTIONS[index]
        self._push_channel = channel
        self.push_key_edit.setText(self.settings.value(f"push_key/{channel}", "") if channel else "")
        self.push_key_edit.setPlaceholderText(hint or "先选择推送渠道")
        for w in (self.push_key_edit, self.push_test_btn, self.push_link):
            w.setEnabled(bool(channel))
        if url:
            self.push_link.setUrl(url)
        self.settings.setValue("push_channel", channel)

    def _test_push(self):
        key = self.push_key_edit.text().strip()
        if not key:
            self.toast("warning", "还没填推送 Key")
            return
        self.settings.setValue(f"push_key/{self._push_channel}", key)
        self.push_test_btn.setEnabled(False)
        w = PushWorker(self._push_channel, key, "GPUHawk 推送测试",
                       f"收到这条说明微信提醒配置成功 · {datetime.now():%H:%M:%S}")
        w.done.connect(self._on_test_push)
        self._run(w)

    def _on_test_push(self, ok: bool, detail: str):
        self.push_test_btn.setEnabled(True)
        name = core.PUSH_CHANNELS.get(self._push_channel, self._push_channel)
        self.log(f"测试推送（{name}）{'成功' if ok else '失败'}，平台返回：{detail}")
        if ok:
            self.toast("success", "平台已接收", f"去微信看看有没有收到。平台返回：{detail}", 10000)
        else:
            self.toast("error", "发送失败", detail, -1)

    # ---------------- 工具

    def log(self, msg: str):
        self.log_view.appendPlainText(f"[{datetime.now():%H:%M:%S}] {msg}")

    def toast(self, kind: str, title: str, content: str = "", duration: int = 3000):
        getattr(InfoBar, kind)(title=title, content=content, orient=Qt.Horizontal, isClosable=True,
                               position=InfoBarPosition.TOP_RIGHT, duration=duration, parent=self)

    def _run(self, worker: QThread):
        self._workers.append(worker)
        worker.finished.connect(lambda: self._workers.remove(worker) if worker in self._workers else None)
        worker.start()

    def _push(self, title: str, content: str):
        """托盘通知 + 响铃 + 可选微信推送（后台线程，不卡界面）。"""
        self.tray.showMessage(title, content, QSystemTrayIcon.Information, 10000)
        QApplication.beep()
        key = self.push_key_edit.text().strip()
        if self._push_channel and key:
            w = PushWorker(self._push_channel, key, title, content)
            w.done.connect(lambda ok, err: None if ok else self.log(f"微信推送失败：{err}"))
            self._run(w)

    def _show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # ---------------- 元数据 / token

    def _load_meta(self):
        w = MetaWorker()
        w.done.connect(self._on_meta)
        w.failed.connect(lambda e: self.log(f"加载型号/地区失败：{e}（点「刷新」重试）"))
        self._run(w)

    def _on_meta(self, catalog: dict, groups: dict, mornai: list):
        self.catalog, self.groups = catalog, groups
        if mornai:
            self.mornai = {d["name"]: d for d in mornai if d.get("name")}
            self.mornai_combo.blockSignals(True)
            self.mornai_combo.clear()
            self.mornai_combo.addItem("不监控", userData="")
            for name, d in self.mornai.items():
                self.mornai_combo.addItem(d.get("shortName") or name, userData=name)
            self.mornai_combo.setCurrentIndex(
                next((i for i in range(self.mornai_combo.count()) if self.mornai_combo.itemData(i) == self._saved_mornai), 0))
            self.mornai_combo.blockSignals(False)
        self.gpu_combo.blockSignals(True)
        self.gpu_combo.clear()
        self.gpu_combo.addItems(list(catalog))
        completer = QCompleter(list(catalog), self.gpu_combo)
        completer.setFilterMode(Qt.MatchContains)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.gpu_combo.setCompleter(completer)
        self.gpu_combo.setText(self._saved_gpu)
        self.gpu_combo.blockSignals(False)
        if not self.region_btns:
            for name in sorted({name for name, _ in groups.values()}):
                btn = PillPushButton(name)
                btn.setChecked(name in self._saved_regions)
                self.region_flow.addWidget(btn)
                self.region_btns.append(btn)
        self.log(f"已加载 AutoDL {len(catalog)} 个型号、{len(groups)} 个地区；晨涧云 {len(self.mornai)} 个型号")
        self._render_table()
        self._refresh_inventory()

    def _save_token(self):
        token = self.token_edit.text().strip()
        if not token:
            self.toast("warning", "token 为空", "先粘贴 authorization 的值")
            return
        self.token = token
        save_token(token)
        self._check_token()

    def _check_token(self):
        self.token_status.setText("验证中…")
        w = TokenWorker(self.token, self.sub_user_sw.isChecked())
        w.ok.connect(self._on_token_ok)
        w.failed.connect(self._on_token_bad)
        self._run(w)

    def _on_token_ok(self, user: dict):
        who = user.get("username") or user.get("nickname") or user.get("phone") or user.get("id") or "已登录"
        exp = core.token_expiry(self.token)
        left = f" · 剩余 {(exp - datetime.now()).days} 天" if exp else ""
        self.user_name = str(who)
        self.token_status.setText(f"✓ {who}{left}")
        self.stat_acct.set(f"{who}{left}")
        if not self.grab_worker:
            self.status_label.setText("待机中 · 选好型号点「开始」")
        if self.groups and not self.inventory:
            self._refresh_inventory()

    def _on_token_bad(self, err: str):
        self.token_status.setText(f"✗ 无效：{err}")
        self.stat_acct.set("token 无效")
        self.toast("error", "token 验证失败", err, 5000)

    # ---------------- 库存表

    def _refresh_inventory(self):
        if not self.groups:
            self._load_meta()
            return
        # 没 token 时只刷晨涧云（免登录）；AutoDL 部分 InventoryWorker 会跳过
        w = InventoryWorker(self.token, self.sub_user_sw.isChecked(), self.groups)
        w.table.connect(self._on_table)
        w.failed.connect(lambda e: self.log(f"刷新库存失败：{e}"))
        self._run(w)

    def _mornai_target(self) -> str:
        return self.mornai_combo.currentData() or ""

    def _targets(self) -> dict[str, str]:
        """{平台: 目标型号}；运行中用启动时的配置，否则用界面当前选择。"""
        cfg = self.grab_worker.cfg if self.grab_worker else None
        if cfg:
            return {AUTODL: cfg.gpu, MORNAI: cfg.mornai_gpu}
        return {AUTODL: self.gpu_combo.text().strip(), MORNAI: self._mornai_target()}

    def _gpu_label(self, platform: str, gpu: str) -> str:
        return (self.mornai.get(gpu, {}).get("shortName") or gpu) if platform == MORNAI else gpu

    def _gpu_mem(self, platform: str, gpu: str) -> str:
        if platform == MORNAI:
            gb = self.mornai.get(gpu, {}).get("gpuMemory")
            return f"{gb} GB" if gb else "-"
        return fmt_gb(self.catalog.get(gpu, 0))

    def _on_table(self, platform: str, region: str, table: dict):
        now = f"{datetime.now():%H:%M:%S}"
        for gpu, info in table.items():
            self.inventory[(platform, region, gpu)] = (info["idle"], info["total"], now)
        self._check_idle_alert(platform, region, table)
        self._render_timer.start()  # 一轮里各地区陆续返回，合并成一次重绘

    def _check_idle_alert(self, platform: str, region: str, table: dict):
        """目标型号从「不够」变成「够」时提醒一次。晨涧云只监控，有 1 张就提醒。"""
        if not self.grab_worker:
            return
        cfg = self.grab_worker.cfg
        gpu = self._targets()[platform]
        if not gpu:
            return
        need = cfg.num if platform == AUTODL else 1
        idle = table.get(gpu, {}).get("idle", 0)
        if idle > 0:
            self.radar.ping()
        label = self._gpu_label(platform, gpu)
        if platform == MORNAI:
            # 云主机、云容器看起来是同一个卡池，按两者中较大的空闲数只提醒一次
            peak = lambda: max((v for (p, _), v in self.prev_idle.items() if p == MORNAI), default=0)
            was = peak()
            self.prev_idle[(platform, region)] = idle
            now = peak()
            if now >= need > was:
                self.log(f"★ 晨涧云 {label} 有 {now} 张空闲（{region}）")
                self._push(f"晨涧云 {label} 有卡了", f"空闲 {now} 张（{region}），快去 mornai.cn 租用")
            return
        was = self.prev_idle.get((platform, region), 0)
        self.prev_idle[(platform, region)] = idle
        if idle >= need > was:
            self.log(f"★ {platform} {region} {label} 有 {idle} 张空闲")
            if cfg.dry_run:
                self._push(f"{label} 有卡了", f"{region} 空闲 {idle} 张，快去 autodl.com 创建实例")

    def _render_table(self):
        targets = self._targets()
        rows = [(p, r, g, *v) for (p, r, g), v in self.inventory.items()
                if (p == MORNAI or v[1] > 0)
                and (not self.only_target_cb.isChecked() or g == targets[p])
                and (not self.only_idle_cb.isChecked() or v[0] > 0)]
        rows.sort(key=lambda x: (-x[3], x[0], x[1], x[2]))
        self._rows = rows
        # 复用已有单元格、填完再统一重绘和算列宽；逐格 setItem + 自动列宽会让界面卡一秒多
        t = self.table
        t.setUpdatesEnabled(False)
        t.setRowCount(len(rows))
        for i, (platform, region, gpu, idle, total, ts) in enumerate(rows):
            vals = (platform, region, self._gpu_label(platform, gpu), self._gpu_mem(platform, gpu),
                    idle, total or "-", ts)
            for j, val in enumerate(vals):
                item = t.item(i, j)
                if item is None:
                    item = QTableWidgetItem()
                    if j >= 3:
                        item.setTextAlignment(Qt.AlignCenter)
                    t.setItem(i, j, item)
                item.setText(str(val))
                item.setData(Qt.BackgroundRole, IDLE_BG if idle > 0 else None)
                if j == 4:
                    f = item.font()
                    f.setBold(idle > 0)
                    item.setFont(f)
        for col in (0, 1, 2):
            t.resizeColumnToContents(col)
        t.setUpdatesEnabled(True)

        if not self.inventory or not any(targets.values()):
            self.stat_idle.set("-")
        else:
            cards, places = self._target_idle()
            self.stat_idle.set_number(cards, "{} 张" + (f" · {places} 处" if places else ""))

    def _target_idle(self, regions: list[str] | None = None) -> tuple[int, int]:
        """目标型号空闲 (张数, 处数)。晨涧云的云主机/云容器像是同一卡池，取较大值、算一处。"""
        targets = self._targets()
        auto = [v[0] for (p, r, g), v in self.inventory.items()
                if p == AUTODL and targets[AUTODL] and g == targets[AUTODL] and v[0] > 0
                and (not regions or r in regions)]
        morn = max((v[0] for (p, r, g), v in self.inventory.items()
                    if p == MORNAI and targets[MORNAI] and g == targets[MORNAI]), default=0)
        return sum(auto) + morn, len(auto) + (1 if morn else 0)

    def _on_round(self, n: int):
        """每轮结束：轮次滚动 + 趋势图记一个点（AutoDL 只算本次监控的地区）。"""
        self.stat_round.set_number(n)
        if self.grab_worker:
            self.trend.add(self._target_idle(self.grab_worker.cfg.region)[0])

    def _open_machines(self, row: int, _col: int):
        if not 0 <= row < len(self._rows):
            return
        platform, region, gpu = self._rows[row][:3]
        if platform == MORNAI:
            QDesktopServices.openUrl(QUrl(f"https://www.mornai.cn/store/detail?name={gpu}"))
            return
        signs = next((s for name, s in self.groups.values() if name == region), None)
        if not signs or not self.token:
            return
        w = MachineWorker(self.token, self.sub_user_sw.isChecked(), signs, gpu)
        dlg = MachineDialog(f"{region} · {gpu} · 显存 {fmt_gb(self.catalog.get(gpu, 0))}", w, self)
        self._run(w)
        dlg.exec()
        for sig in (w.done, w.failed):
            try:
                sig.disconnect()
            except (RuntimeError, TypeError):
                pass

    # ---------------- 开始 / 停止

    def _toggle(self):
        if self.grab_worker:
            self.grab_worker.stop()
            self.log("正在停止…")
            self.start_btn.setEnabled(False)
            return
        self._start()

    def _start(self):
        gpu = self.gpu_combo.text().strip()
        mornai_gpu = self._mornai_target()
        buy = self.mode_seg.currentRouteKey() == "buy"
        if not gpu and not mornai_gpu:
            self.toast("warning", "还没选型号", "AutoDL 和晨涧云至少选一个")
            return
        if buy and not gpu:
            self.toast("warning", "自动开机只支持 AutoDL", "选一个 AutoDL 型号，或切到「只监控提醒」")
            return
        if gpu:
            if not self.token:
                self.toast("warning", "还没有 AutoDL token", "到「设置」里填写并保存；只看晨涧云可以把 AutoDL 型号清空")
                self.switchTo(self.settings_page)
                return
            if not self.groups:
                self.toast("warning", "地区列表还没加载好", "稍等或点「刷新」")
                return
            if gpu not in self.catalog:
                self.toast("warning", "未知型号", f"{gpu!r} 不在 AutoDL 型号列表里，请从下拉中选")
                return
        regions = self._checked_regions()
        cfg = SimpleNamespace(
            gpu=gpu, mornai_gpu=mornai_gpu, num=self.num_spin.value(), max_price=self.price_spin.value(),
            region=regions or None, image=self.image_edit.text().strip() or core.DEFAULT_IMAGE,
            private_image=self.private_edit.text().strip(), interval=self.interval_spin.value(),
            dry_run=self.mode_seg.currentRouteKey() != "buy", sub_user=self.sub_user_sw.isChecked(),
        )
        if not cfg.dry_run:
            price = f"{cfg.max_price:.2f} 元/时" if cfg.max_price else "不限"
            box = MessageBox(
                "确认自动开机",
                f"发现空闲后会立即按量计费开机（只开 1 台，成功即停止）。\n\n"
                f"型号：{gpu} × {cfg.num}\n价格上限：{price}\n地区：{'、'.join(regions) or '全部'}",
                self)
            box.yesButton.setText("开始抢卡")
            box.cancelButton.setText("取消")
            if not box.exec():
                return
        self._save_settings()
        self.prev_idle.clear()
        self.grab_worker = GrabWorker(self.token, cfg, self.groups)
        self.grab_worker.log.connect(self.log)
        self.grab_worker.table.connect(self._on_table)
        self.grab_worker.round_done.connect(self._on_round)
        self.trend.clear()
        self.grab_worker.succeeded.connect(self._on_success)
        self.grab_worker.auth_failed.connect(self._on_auth_failed)
        self.grab_worker.finished.connect(self._on_stopped)
        self.only_target_cb.setChecked(True)
        self._set_running(True, cfg)
        mode = "只监控" if cfg.dry_run else "自动开机"
        self.log(f"开始（{mode}）：{self._target_text(cfg)}，间隔 {cfg.interval}s")
        self.grab_worker.start()

    def _target_text(self, cfg: SimpleNamespace) -> str:
        parts = []
        if cfg.gpu:
            parts.append(f"AutoDL {cfg.gpu} × {cfg.num}（{'、'.join(cfg.region or []) or '全部地区'}）")
        if cfg.mornai_gpu:
            parts.append(f"晨涧云 {self._gpu_label(MORNAI, cfg.mornai_gpu)}")
        return " + ".join(parts)

    def _set_running(self, running: bool, cfg: SimpleNamespace | None = None):
        for w in (self.target_card, self.region_card, self.mode_seg):
            w.setEnabled(not running)
        (self.radar.start if running else self.radar.stop)()
        self.start_btn.setEnabled(True)
        self.start_btn.setText("停止" if running else "开始")
        self.start_btn.setIcon(FIF.PAUSE if running else FIF.PLAY)
        if running:
            state = "监控中" if cfg.dry_run else "抢卡中"
            self.stat_state.set(state)
            self.stat_round.set_number(0)
            self.status_label.setText(f"{state} · {self._target_text(cfg)} · 关闭窗口会缩到托盘继续运行")
        else:
            self.stat_state.set("待机")
            self.status_label.setText("待机中 · 选好型号点「开始」")

    def _on_stopped(self):
        self.grab_worker = None
        self._set_running(False)
        self.log("已停止")
        if self._quitting:
            QApplication.quit()

    def _on_success(self, r: dict):
        cfg = self.grab_worker.cfg
        msg = f"{r['region']} {cfg.gpu} × {cfg.num}\n机器 {r['machine_id']} · ¥{r['price']:.2f}/时\n已开始计费"
        self.log("✔ 抢卡成功：" + msg.replace("\n", " | "))
        self._push("AutoDL 抢卡成功", msg)
        self._show_window()
        self.toast("success", "抢卡成功", msg.replace("\n", "  "), -1)
        self.confetti.burst()
        QTimer.singleShot(1800, lambda: self._ask_open_console(msg))

    def _ask_open_console(self, msg: str):
        box = MessageBox("抢卡成功", msg + "\n\n现在去控制台查看实例？", self)
        box.yesButton.setText("打开控制台")
        box.cancelButton.setText("稍后")
        if box.exec():
            QDesktopServices.openUrl(QUrl("https://www.autodl.com/console/instance/list"))

    def _on_auth_failed(self, err: str):
        self.log(f"token 失效：{err}")
        self._on_token_bad(err)
        self._push("AutoDL 抢卡已停止", f"token 失效：{err}，请重新获取")

    # ---------------- 关闭

    def closeEvent(self, event):
        if self.grab_worker and not self._quitting:
            event.ignore()
            self.hide()
            self.tray.showMessage(APP_NAME, "仍在后台运行，右键托盘图标可退出", QSystemTrayIcon.Information, 3000)
            return
        self._save_settings()
        event.accept()
        QApplication.quit()

    def _quit(self):
        self._quitting = True
        if self.grab_worker:
            self.grab_worker.stop()  # 线程结束后 _on_stopped 会退出程序
        else:
            self._save_settings()
            QApplication.quit()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
