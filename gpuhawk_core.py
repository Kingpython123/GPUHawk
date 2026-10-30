"""GPUHawk 核心：AutoDL / 晨涧云接口封装 + 命令行单型号抢卡。

命令行盯住 AutoDL 某一款 GPU，一有空闲就按量计费开一台实例，开到即退出。
AutoDL 接口来自网页前端（参考 knight02-bit/AutoDL-monitor、nicholasdezai/autodl-radar），
需要网页登录态的 JWT（浏览器 F12 → 任意 /api/v1/ 请求 → 请求头 authorization），
开发者 Token 不行。晨涧云的库存接口免登录。

用法：
    set AUTODL_TOKEN=eyJ...            (或写进同目录 .env：AUTODL_TOKEN=eyJ...)
    python gpuhawk_core.py --gpu "RTX 4090" --dry-run      # 先只看库存，不下单
    python gpuhawk_core.py --gpu "RTX 4090" --num 1 --max-price 2.5
    python gpuhawk_core.py --list-gpus                     # 列出可选型号名

注意：开机成功即开始按量扣费，脚本只会开一台，成功后立即退出。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

# 打包成 exe 后 __file__ 在临时解压目录，.env 要放在 exe 旁边
APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
ENV_FILE = APP_DIR / ".env"
BASE_URL = "https://www.autodl.com/api/v1"
DEFAULT_IMAGE = "hub.kce.ksyun.com/autodl-image/torch:cuda12.4-cudnn-devel-ubuntu22.04-py312-torch2.5.1"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def load_token() -> str:
    token = os.environ.get("AUTODL_TOKEN", "").strip()
    env_file = ENV_FILE
    if not token and env_file.exists():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "AUTODL_TOKEN":
                token = value.strip().strip('"').strip("'")
    return token


class AuthError(RuntimeError):
    pass


class AutoDL:
    def __init__(self, token: str, sub_user: bool = False) -> None:
        self.prefix = "/sub_user" if sub_user else ""
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": token,
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Referer": "https://www.autodl.com/create",
        })

    def _call(self, method: str, path: str, *, user_scoped: bool = True, **kw):
        url = BASE_URL + (self.prefix if user_scoped else "") + path
        resp = self.s.request(method, url, timeout=15, **kw)
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") == "Success":
            return body.get("data")
        if body.get("code") == "AuthorizeFailed":
            raise AuthError(body.get("msg") or "登录态失效")
        raise RuntimeError(f"{body.get('code')}: {body.get('msg')}")

    def gpu_catalog(self) -> dict[str, int]:
        """{型号: 显存字节数}，按官网排序。"""
        data = self._call("GET", "/machine/gpu_type", user_scoped=False) or []
        return {d["gpu_name"]: int(d.get("gpu_memory") or 0) for d in data if d.get("gpu_name")}

    def gpu_models(self) -> list[str]:
        return list(self.gpu_catalog())

    def region_groups(self) -> dict[str, tuple[str, list[str]]]:
        """{data_center: (地区名, [region_sign, ...])}，与创建页的「选择地区」一致。"""
        groups: dict[str, tuple[str, list[str]]] = {}
        for r in self._call("GET", "/region/list", user_scoped=False) or []:
            if r.get("used_for") != "all" or r.get("visible") != "all":
                continue
            dc = r["data_center"]
            name, signs = groups.setdefault(dc, (r.get("region_name") or dc, []))
            signs.append(r["region_sign"])
        return groups

    def user(self) -> dict:
        return self._call("GET", "/user/detail", user_scoped=False) or {}

    def gpu_table(self, signs: list[str]) -> dict[str, dict[str, int]]:
        """该地区各型号 {型号: {"idle": 空闲, "total": 总数}}。"""
        data = self._call("POST", "/machine/region/gpu_type", json={"region_sign_list": signs}) or []
        table: dict[str, dict[str, int]] = {}
        for group in data:
            if not isinstance(group, dict):
                continue
            for gpu, info in group.items():
                if isinstance(info, dict):
                    table[gpu] = {"idle": int(info.get("idle_gpu_num") or 0),
                                  "total": int(info.get("total_gpu_num") or 0)}
        return table

    def idle_count(self, signs: list[str], gpu: str) -> int:
        return self.gpu_table(signs).get(gpu, {}).get("idle", 0)

    def _machine_page(self, signs: list[str], gpu: str, num: int, page: int = 1) -> dict:
        """机器列表一页：{list, max_page, result_total, ...}。"""
        return self._call("POST", "/user/machine/list", json={
            "charge_type": "payg",
            "default_order": True,
            "gpu_idle_num": num,
            "gpu_type_name": [gpu],
            "page_index": page,
            "page_size": 50,
            "region_sign_list": signs,
        }) or {}

    def machines(self, signs: list[str], gpu: str, num: int) -> list[dict]:
        data = self._machine_page(signs, gpu, num)
        result = [m for m in data.get("list") or [] if (m.get("gpu_idle_num") or 0) >= num]
        return sorted(result, key=lambda m: m.get("payg_price") or 0)

    def find_machine(self, signs: list[str], gpu: str, machine_id: str, max_pages: int = 20) -> dict | None:
        """按 machine_id 在「地区 + 型号」的机器列表里找一台，会翻页；找不到（下架/改型号）返回 None。"""
        page = 1
        while True:
            data = self._machine_page(signs, gpu, 0, page)
            for m in data.get("list") or []:
                if m.get("machine_id") == machine_id:
                    return m
            if page >= min(int(data.get("max_page") or 1), max_pages):
                return None
            page += 1

    def create_payg(self, machine_id: str, num: int, image: str, private_image_uuid: str):
        return self._call("POST", "/order/instance/create/payg", json={
            "instance_info": {
                "charge_type": "payg",
                "expand_data_disk": 0,
                "image": image,
                "machine_id": machine_id,
                "req_gpu_amount": num,
                "private_image_uuid": private_image_uuid,
            },
            "price_info": {
                "charge_type": "payg",
                "expand_data_disk": 0,
                "machine_id": machine_id,
                "num": num,
            },
        })


PUSH_CHANNELS = {
    "serverchan": "Server酱",
    "pushplus": "PushPlus",
    "wxpusher": "WxPusher",
    "wecom": "企业微信群机器人",
}


def push(channel: str, key: str, title: str, content: str) -> tuple[bool, str]:
    """推送到微信，返回 (是否成功, 平台返回的说明)。"""
    key = key.strip()
    try:
        if channel == "serverchan":
            # Server酱³ 的 key 形如 sctp<uid>t...，走单独域名
            m = re.match(r"sctp(\d+)t", key)
            url = (f"https://{m.group(1)}.push.ft07.com/send/{key}.send" if m
                   else f"https://sctapi.ftqq.com/{key}.send")
            r = requests.post(url, data={"title": title, "desp": content}, timeout=10).json()
            ok = r.get("code") == 0
        elif channel == "pushplus":
            r = requests.post("https://www.pushplus.plus/send", timeout=10, json={
                "token": key, "title": title, "content": content, "template": "txt"}).json()
            ok = r.get("code") == 200
        elif channel == "wxpusher":
            r = requests.post("https://wxpusher.zjiecode.com/api/send/message/simple-push", timeout=10, json={
                "spt": key, "summary": title, "content": f"{title}\n{content}", "contentType": 1}).json()
            ok = r.get("code") == 1000 or r.get("success") is True
        elif channel == "wecom":
            url = key if key.startswith("http") else f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}"
            r = requests.post(url, timeout=10, json={
                "msgtype": "text", "text": {"content": f"{title}\n{content}"}}).json()
            ok = r.get("errcode") == 0
        else:
            return False, f"未知推送渠道：{channel}"
    except (requests.RequestException, ValueError) as exc:
        return False, f"网络错误：{exc}"
    # 成功也带上平台返回（PushPlus 的 data 是消息流水号，可以去它的发送记录里对照）
    return ok, json.dumps(r, ensure_ascii=False)[:300]


def notify(title: str, content: str, channel: str = "", key: str = "", popup: bool = True) -> None:
    """响铃 + Windows 弹窗（不阻塞）+ 可选微信推送。"""
    print("\a", end="", flush=True)
    if popup and sys.platform == "win32":
        import ctypes
        import winsound
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        threading.Thread(
            target=ctypes.windll.user32.MessageBoxW,
            args=(0, content, title, 0x40 | 0x40000),  # MB_ICONINFORMATION | MB_TOPMOST
            daemon=False,
        ).start()
    if channel and key:
        ok, err = push(channel, key, title, content)
        if not ok:
            log(f"{PUSH_CHANNELS.get(channel, channel)} 推送失败：{err}")


MORNAI_API = "https://api.mornai.cn/app-api"
MORNAI_KINDS = {"cloud": "云主机", "container": "云容器"}


class MornAI:
    """晨涧云（mornai.cn）。库存接口不需要登录；这里只做监控，不下单。"""

    def __init__(self) -> None:
        self.s = requests.Session()
        self.s.headers.update({"tenant-id": "1", "User-Agent": USER_AGENT, "Referer": "https://www.mornai.cn/"})

    def gpu_counts(self, kind: str = "cloud") -> list[dict]:
        """[{name, shortName, gpuMemory(GB), count(空闲)}]；kind 为 cloud（云主机）或 container（云容器）。"""
        resp = self.s.get(f"{MORNAI_API}/{kind}/resource/gpu/count", timeout=15)
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") != 0:
            raise RuntimeError(f"晨涧云：{body.get('msg') or body.get('code')}")
        return body.get("data") or []

    def gpu_table(self, kind: str = "cloud") -> dict[str, dict[str, int]]:
        """与 AutoDL.gpu_table 同形状；晨涧云不给总数，total 记 0。"""
        return {d["name"]: {"idle": int(d.get("count") or 0), "total": 0} for d in self.gpu_counts(kind) if d.get("name")}


def token_expiry(token: str) -> datetime | None:
    """不验签读 JWT 的 exp，只用于提示剩余天数。"""
    import base64
    import json
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return datetime.fromtimestamp(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except Exception:  # noqa: BLE001 - 解析不了就当未知
        return None


def try_grab(api: AutoDL, args, groups, log=log, on_table=None) -> dict | None:
    """扫一轮，抢到返回成功信息，否则 None。on_table(地区名, 各型号库存) 供界面显示。"""
    for dc, (name, signs) in groups.items():
        if args.region and name not in args.region:
            continue
        table = api.gpu_table(signs)
        if on_table:
            on_table(name, table)
        idle = table.get(args.gpu, {}).get("idle", 0)
        if idle < args.num:
            continue
        log(f"{name} {args.gpu} 空闲 {idle} 张")
        for m in api.machines(signs, args.gpu, args.num):
            price = (m.get("payg_price") or 0) / 1000
            if args.max_price and price > args.max_price:
                log(f"  跳过 {m['machine_id']}：¥{price:.2f}/h 超过上限 ¥{args.max_price}")
                continue
            if args.dry_run:
                log(f"  [dry-run] 可开 {m['machine_id']} ¥{price:.2f}/h（未下单）")
                continue
            log(f"  尝试开机 {m['machine_id']} ¥{price:.2f}/h ...")
            try:
                data = api.create_payg(m["machine_id"], args.num, args.image, args.private_image)
            except RuntimeError as exc:
                log(f"  失败：{exc}")
                continue
            return {"region": name, "machine_id": m["machine_id"], "price": price, "data": data}
    return None


def main() -> int:
    p = argparse.ArgumentParser(description="AutoDL 单型号 GPU 抢卡")
    p.add_argument("--gpu", help='型号名，需与官网一致，如 "RTX 4090"、"RTX 5090"')
    p.add_argument("--num", type=int, default=1, help="单实例卡数（默认 1）")
    p.add_argument("--max-price", type=float, default=0, help="每卡单价上限 元/小时，0 不限")
    p.add_argument("--region", action="append", help='只抢这些地区，如 --region 北京B区 --region 西北B区')
    p.add_argument("--image", default=DEFAULT_IMAGE, help="公共镜像地址")
    p.add_argument("--private-image", default="", help="私有镜像 UUID（填了优先用）")
    p.add_argument("--interval", type=float, default=15, help="轮询间隔秒（默认 15，加随机抖动）")
    p.add_argument("--sub-user", action="store_true", help="子账号 token 用这个")
    p.add_argument("--push", choices=list(PUSH_CHANNELS), help="微信推送渠道（可选）")
    p.add_argument("--push-key", default=os.environ.get("PUSH_KEY", ""), help="推送渠道的 key / token / webhook")
    p.add_argument("--sckey", default=os.environ.get("SCKEY", ""), help="Server酱 SendKey，等同 --push serverchan")
    p.add_argument("--dry-run", action="store_true", help="只监控不下单")
    p.add_argument("--list-gpus", action="store_true", help="列出官网型号名后退出")
    args = p.parse_args()

    token = load_token()
    api = AutoDL(token, sub_user=args.sub_user)

    models = api.gpu_models()
    if args.list_gpus:
        print("\n".join(models))
        return 0
    if not args.gpu:
        p.error("请用 --gpu 指定型号（--list-gpus 查看可选值）")
    if args.gpu not in models:
        p.error(f"未知型号 {args.gpu!r}，可选：{', '.join(models)}")
    if not token:
        p.error("没有 token：设置环境变量 AUTODL_TOKEN 或在同目录 .env 写 AUTODL_TOKEN=...")

    channel, key = ("serverchan", args.sckey) if args.sckey else (args.push or "", args.push_key)
    groups = api.region_groups()
    mode = "只监控" if args.dry_run else "监控 + 自动开机"
    log(f"{mode}：{args.gpu} x{args.num}，{len(groups)} 个地区，间隔 {args.interval}s，Ctrl+C 退出")

    rounds = 0
    while True:
        rounds += 1
        try:
            result = try_grab(api, args, groups)
        except AuthError as exc:
            log(f"token 失效：{exc}，请重新从浏览器复制")
            notify("AutoDL 抢卡停止", f"token 失效：{exc}", channel, key)
            return 2
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            log(f"本轮出错：{exc}")
            result = None

        if result:
            msg = (f"{result['region']} {args.gpu} x{args.num}\n"
                   f"机器 {result['machine_id']}  ¥{result['price']:.2f}/h\n"
                   f"已开始计费，去控制台查看：https://www.autodl.com/console/instance/list")
            log("抢卡成功！" + msg.replace("\n", " | "))
            notify("AutoDL 抢卡成功", msg, channel, key)
            return 0

        if rounds % 20 == 0:
            log(f"已轮询 {rounds} 轮，暂无 {args.gpu}")
        time.sleep(args.interval * random.uniform(0.8, 1.3))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("已退出")
