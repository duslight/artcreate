"""artcreate · LLM 客户端：文本 chat（编译/自检）+ VLM（意图回查），OpenAI 兼容协议"""
import json
import urllib.request

from .config import get_config


def _chat(conf: dict, messages: list, temperature: float = 0.7,
          timeout: int = 60) -> str:
    body = json.dumps({
        "model": conf["model"],
        "messages": messages,
        "temperature": temperature,
    }).encode()
    req = urllib.request.Request(
        conf["base_url"] + "chat/completions", data=body, method="POST",
        headers={"Authorization": f"Bearer {conf['api_key']}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.loads(r.read().decode())
    return resp["choices"][0]["message"]["content"]


def text_chat(user_prompt: str, temperature: float = 0.7, timeout: int = 60) -> str:
    """编译器/自检通道（glm-4-flash）"""
    conf = get_config().llm_conf("compiler")
    return _chat(conf, [{"role": "user", "content": user_prompt}],
                 temperature, timeout)


def vlm_chat(image_b64: str, question: str, timeout: int = 90) -> str:
    """VLM 意图回查通道（glm-4v-flash，D18-L3 / D20 已验证）。
    image_b64: 裸 base64（不带 data: 前缀）。"""
    conf = get_config().llm_conf("vlm")
    messages = [{"role": "user", "content": [
        {"type": "image_url",
         "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
        {"type": "text", "text": question},
    ]}]
    return _chat(conf, messages, temperature=0.1, timeout=timeout)
