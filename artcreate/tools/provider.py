"""artcreate · Provider 适配层：generate(prompt, size, count, ref_images) 统一接口

相对 art_pipeline 版本的升级（D19/D20 实测落地）：
- 支持参考图（URL / 本地路径 → base64 data URI；单张或列表 ≤14 张）
- 参考图模式尺寸约束由 config.validate_size 把关（≥369万像素）
- 保留 LibLib 双钥签名通道（备援）
"""
import base64
import hashlib
import hmac
import json
import mimetypes
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import get_config


class ProviderError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _to_data_uri(ref) -> str:
    """本地路径 → base64 data URI；URL/已是 data URI → 原样返回。"""
    if isinstance(ref, str) and (ref.startswith("http") or ref.startswith("data:")):
        return ref
    p = Path(ref)
    mime = mimetypes.guess_type(p.name)[0] or "image/png"
    b64 = base64.b64encode(p.read_bytes()).decode()
    return f"data:{mime};base64,{b64}"


def generate(prompt: str, size: str, count: int = 1, ref_images=None):
    """生成图 URL 列表。ref_images: None | str | list[str]（路径或 URL，≤14 张）。"""
    cfg = get_config()
    pid = cfg.active_provider
    conf = cfg.provider_conf(pid)
    if pid == "ark":
        return _gen_ark(conf, prompt, size, count, ref_images)
    if pid == "liblib":
        if ref_images:
            raise ProviderError("LIBLIB_NO_I2I", "LibLib 通道不支持参考图")
        return _gen_liblib(conf, prompt, count)
    raise ProviderError("UNKNOWN_PROVIDER", f"未知 provider: {pid}")


def _gen_ark(conf, prompt, size, count, ref_images):
    cfg = get_config()
    image_param = None
    if ref_images:
        refs = [ref_images] if isinstance(ref_images, str) else list(ref_images)
        if len(refs) > 14:
            raise ProviderError("TOO_MANY_REFS", "参考图上限 14 张（D19 实测）")
        cfg.validate_size(size, with_ref=True)
        image_param = refs[0] if len(refs) == 1 else [_to_data_uri(r) for r in refs]

    urls = []
    for _ in range(count):
        body = {"model": conf["model"], "prompt": prompt, "size": size,
                "response_format": "url", "watermark": conf.get("watermark", False)}
        if image_param:
            body["image"] = image_param
        req = urllib.request.Request(
            conf["endpoint"], data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {conf['api_key']}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                resp = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read().decode()).get("error", {})
            except Exception:
                err = {"code": f"HTTP_{e.code}", "message": str(e)}
            raise ProviderError(err.get("code", "ARK_ERROR"),
                                err.get("message", "方舟调用失败"))
        data = resp.get("data") or []
        if not data or not data[0].get("url"):
            raise ProviderError("ARK_NO_IMAGE", "方舟未返回图片")
        urls.append(data[0]["url"])
    return urls


# ---------- LibLib（双钥签名 + 任务轮询，备援通道） ----------
def _liblib_call(conf, path, body=None, method="POST"):
    ts = str(int(time.time() * 1000))
    nonce = f"{time.time_ns():x}"
    content = "&".join((path, ts, nonce))
    digest = hmac.new(conf["secret_key"].encode(), content.encode(),
                      hashlib.sha1).digest()
    sign = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    qs = (f"AccessKey={conf['access_key']}&Signature={sign}"
          f"&Timestamp={ts}&SignatureNonce={nonce}")
    req = urllib.request.Request(
        f"{conf['endpoint']}{path}?{qs}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def _gen_liblib(conf, prompt, count):
    w, h = conf["fixed_size"]
    resp = _liblib_call(conf, "/api/generate/webui/text2img", {
        "templateUuid": "",
        "generateParams": {"prompt": prompt, "width": w, "height": h,
                           "batch": count}})
    guid = (resp.get("data") or {}).get("generateUuid")
    if not guid:
        raise ProviderError("LIBLIB_SUBMIT", f"LibLib 提交失败: {resp.get('msg')}")
    deadline = time.time() + 300
    while time.time() < deadline:
        time.sleep(conf.get("poll_interval", 5))
        st = _liblib_call(conf, "/api/generate/webui/status",
                          {"generateUuid": guid}).get("data") or {}
        status = st.get("generateStatus")
        if status == 5:
            urls = [im["imageUrl"] for im in st.get("images", [])
                    if im.get("imageUrl")]
            if not urls:
                raise ProviderError("LIBLIB_NO_IMAGE", "LibLib 未返回图片")
            return urls
        if status in (4,):
            raise ProviderError("LIBLIB_FAILED",
                                st.get("generateMsg") or "LibLib 生成失败")
    raise ProviderError("LIBLIB_TIMEOUT", "LibLib 轮询超时")
