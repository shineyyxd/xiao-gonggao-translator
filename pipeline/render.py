# -*- coding: utf-8 -*-
"""大字版文字稿 HTML 渲染：手机宽、超大字体、高对比、卡片式，适合老人手机看。

合规话术每期只出现两次：开头一次、结尾一次（正文三段由契约保证不带）。
查证链接缩到 14px 淡色并标注"查证用（可忽略）"（老人反馈：长网址以为是正文）。
"""
import html

from config import DISCLAIMER

PRODUCT_NAME = "小公告翻译官（银发向）"

_CSS = """
body{margin:0;padding:12px;background:#ffffff;color:#111111;
     font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;}
.page{max-width:480px;margin:0 auto;}
h1{font-size:26px;line-height:1.5;margin:8px 0;}
.date{font-size:22px;color:#333;}
.disclaimer{font-size:20px;line-height:1.8;background:#fff3cd;border:2px solid #e0a800;
            border-radius:10px;padding:12px;margin:14px 0;font-weight:bold;}
.card{border:2px solid #222;border-radius:12px;padding:14px;margin:16px 0;}
.company{font-size:24px;font-weight:bold;margin:0 0 6px;}
.meta{font-size:20px;color:#555;margin-bottom:8px;}
.line1{font-size:23px;line-height:1.8;font-weight:bold;margin:8px 0;}
.line2{font-size:21px;line-height:1.9;margin:8px 0;}
.line3{font-size:20px;line-height:1.8;color:#333;margin:8px 0;}
.link{font-size:14px;line-height:1.6;word-break:break-all;color:#9aa0a6;}
.link a{color:#9aa0a6;}
"""

# 卡片抬头公告名超过该长度截断加"…"（完整名保留在出处行）
CARD_TITLE_MAX = 20


def _esc(s) -> str:
    return html.escape(str(s or ""), quote=True)


def _short_title(title: str) -> str:
    """公告名超 CARD_TITLE_MAX 字截断加省略号（老人反馈：长标题直接放弃阅读）。"""
    title = (title or "").strip()
    return title if len(title) <= CARD_TITLE_MAX else title[:CARD_TITLE_MAX] + "…"


def render_simple(run_date: str, title: str, message: str) -> str:
    """简版大字页：用于 EMPTY_DAY（今日无重要公告）与 STOPPED（停刊）路径。"""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{PRODUCT_NAME} {html.escape(run_date)}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="page">
  <h1>{PRODUCT_NAME}</h1>
  <p class="date">{html.escape(run_date)}</p>
  <div class="disclaimer">{html.escape(DISCLAIMER)}</div>
  <div class="card">
    <p class="company">{html.escape(title)}</p>
    <p class="line2">{html.escape(message)}</p>
  </div>
  <div class="disclaimer">{html.escape(DISCLAIMER)}</div>
</div>
</body>
</html>
"""


def render_html(run_date: str, items: list) -> str:
    """items: [{company,title,date,link,line1,line2,line3,score,sector,event_type}]"""
    cards = []
    for i, it in enumerate(items, 1):
        cards.append(f"""
  <div class="card">
    <p class="company">{i}. {_esc(it['company'])}</p>
    <p class="meta">{_esc(it['sector'])} · {_esc(it['event_type'])} · {_esc(_short_title(it.get('title')))}</p>
    <p class="line1">{_esc(it['line1'])}</p>
    <p class="line2">{_esc(it['line2'])}</p>
    <p class="line3">{_esc(it['line3'])}</p>
    <p class="link">查证用（可忽略）：<a href="{_esc(it['link'])}">{_esc(it['link'])}</a></p>
  </div>""")
    body = "\n".join(cards)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{PRODUCT_NAME} {html.escape(run_date)}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="page">
  <h1>{PRODUCT_NAME}</h1>
  <p class="date">{html.escape(run_date)}</p>
  <div class="disclaimer">{html.escape(DISCLAIMER)}</div>
  {body}
  <div class="disclaimer">{html.escape(DISCLAIMER)}</div>
</div>
</body>
</html>
"""
