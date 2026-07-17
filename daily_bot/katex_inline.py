#!/usr/bin/env python3
"""
katex_inline —— 生成【完全自包含】的 KaTeX head 块（离线可渲染）。

背景：deep-study / 组合报告的 HTML 原先从 CDN 加载 KaTeX（css+js+webfonts），
学长本地/离线打开时 CDN 不可达 → JS 不加载则公式全成原文，或字体不加载则根号/大算符/
关系符渲染破碎。修复：把 katex.min.css（字体以 base64 data: URI 内嵌）+ katex.min.js +
auto-render.min.js + 渲染配置全部内联进文档，零外部依赖。

资产一次性 vendored 在 daily_bot/assets/katex/（见该目录）。本模块把它们拼成一个 head 块，
memoize 复用（每次构建只读盘一次）。woff2 覆盖所有现代浏览器；css 里的 woff/ttf 相对引用
浏览器命中 woff2 后不会再取，无 404。
"""

import base64
import functools
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_KDIR = os.path.join(_HERE, "assets", "katex")

# 渲染配置：4 种定界符，throwOnError 容错；内联脚本无 defer，故等 DOMContentLoaded 再渲染
_CONFIG_JS = (
    "document.addEventListener('DOMContentLoaded',function(){"
    "renderMathInElement(document.body,{delimiters:["
    "{left:'$$',right:'$$',display:true},"
    "{left:'\\\\[',right:'\\\\]',display:true},"
    "{left:'$',right:'$',display:false},"
    "{left:'\\\\(',right:'\\\\)',display:false}"
    "],throwOnError:false});});"
)


@functools.lru_cache(maxsize=1)
def _css_with_fonts():
    """把 katex.min.css 里的 woff2 字体 url() 换成 base64 data: URI。"""
    with open(os.path.join(_KDIR, "katex.min.css"), encoding="utf-8") as f:
        css = f.read()

    def repl(m):
        fn = m.group(1).strip("'\"").split("/")[-1]
        p = os.path.join(_KDIR, "fonts", fn)
        if not os.path.exists(p):
            return m.group(0)
        with open(p, "rb") as ff:
            b64 = base64.b64encode(ff.read()).decode("ascii")
        return f"url(data:font/woff2;base64,{b64})"

    return re.sub(r"url\(([^)]*\.woff2)\)", repl, css)


@functools.lru_cache(maxsize=1)
def head_block():
    """返回自包含的 <style>+<script> KaTeX head 块（可直接放进文档 <head>）。"""
    css = _css_with_fonts()
    with open(os.path.join(_KDIR, "katex.min.js"), encoding="utf-8") as f:
        js = f.read()
    with open(os.path.join(_KDIR, "auto-render.min.js"), encoding="utf-8") as f:
        ar = f.read()
    return (f"<style>{css}</style>\n"
            f"<script>{js}</script>\n"
            f"<script>{ar}</script>\n"
            f"<script>{_CONFIG_JS}</script>")
