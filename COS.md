# Bot 成果交付链路：从 HTML 到成员打开

> 适用范围：任何"生成一个 HTML 成果 → 发到企业微信群 → 成员打开阅读/下载"的 bot。
> 本文档以 PaperRAG（论文精读 bot）的实现为例，梳理整条交付链路的设计与理由，供其它 bot 参考复用。

## 整体流程

```
本地生成 HTML 成果
      ↓
① 上传到腾讯云 COS（私有桶）
      ↓
② 一次上传 → 生成两个预签名链接（预览 inline / 下载 attachment）
      ↓
③ 企业微信群机器人推送：成果介绍 + 两个链接
      ↓
④ 成员点"预览"即读，或点"下载"存 HTML 到本地永久保存
```

核心思路：成果文件本身不进企业微信（企微对文件类型/大小有限制，且不适合承载富文本 HTML），而是**存到对象存储（COS），群里只发链接**。链接分"预览"和"下载"两种，兼顾"点开即读"和"存档永久保存"。

---

## ① 上传到腾讯云 COS（私有桶）

把本地 HTML 上传到 COS 的指定前缀下（如 `paper_rag/`），存为 `text/html` 类型（便于后续浏览器直接内联预览）。

关键点：

- **私有桶**：桶不公开，对象不能被匿名访问——必须靠"预签名 URL"（下一步）才能访问，避免成果被任意抓取。
- **子账号最小权限**：用一个只对指定前缀（`paper_rag/`）有读写权限的子账号，即使密钥泄露，影响面也限制在这个前缀内。
- **配置全部走环境变量**（`.env`，不进 git）：`COS_SECRET_ID` / `COS_SECRET_KEY` / `COS_REGION` / `COS_BUCKET` / `COS_PREFIX`。密钥绝不硬编码进代码。

```python
client.put_object(
    Bucket=bucket,
    Key=prefix + object_name,          # 如 paper_rag/2026-08-06_xxx.html
    Body=html_bytes,
    ContentType="text/html; charset=utf-8",
)
```

---

## ② 一次上传 → 两个预签名链接（这是设计的关键）

**同一个对象只上传一次**，但生成**两个**指向它的预签名 URL——靠在 URL 里覆盖"响应头"实现，不需要存两份文件。

| 链接 | 用途 | 关键参数 |
|---|---|---|
| **预览（preview）** | 浏览器直接打开阅读 | `response-content-disposition=inline`，Content-Type 保持 `text/html` |
| **下载（download）** | 强制下载、文件名友好 | `response-content-disposition=attachment; filename="..."` + `response-content-type=application/octet-stream` |

**为什么要两个链接：**

- **预览**：方便在群里点开即读，不必先下载。
- **下载**：预签名 URL 有有效期（默认 30 天）会过期；下载让成员把 HTML 存到本地**永久保存**，不依赖会过期的链接。

**一个关键的坑**（下载链接必须处理）：只设 `attachment` 是不够的——如果 Content-Type 仍是 `text/html`，浏览器会忽略 `attachment` 而直接内联渲染。**必须同时把 `response-content-type` 覆盖成不可渲染的类型（`application/octet-stream`），浏览器才会真正强制下载。**

```python
# 预览：只 inline，Content-Type 仍是 text/html → 浏览器内联显示
preview = sign(key, expires, "inline")

# 下载：attachment + 覆盖 Content-Type → 浏览器强制下载
download = sign(key, expires,
                f'attachment; filename="{object_name}"',
                content_type="application/octet-stream")
```

**有效期**：预签名 URL 默认 30 天（`expires=2592000` 秒）。过期后链接失效——这正是"下载"链接存在的意义：成员应尽早下载存档，而不是长期依赖群里的链接。

---

## ③ 企业微信群机器人推送

用群机器人的 webhook 发一条 **markdown 消息**，内容 = 成果介绍 + 两个链接。

**注意：用的是 markdown 消息，不是原生"卡片(template_card)"。** 群里看到的标题、加粗、链接样式，都是 markdown 渲染的（群机器人 webhook 不支持 template_card；那需要企业微信应用的 access_token 那套，是另一回事）。

markdown 支持的语法有限：`# 标题`、`**加粗**`、`> 引用`、`[文字](链接)`、`<font color="info|comment|warning">变色</font>`（仅这三种颜色）。单条上限约 4096 字节。

```python
md = (f"### 📄 {title}\n"
      f'> 方向：<font color="info">{area}</font>\n\n'
      f"{summary}\n\n"
      f"[在线预览]({preview_url}) ｜ [下载]({download_url})")
send_markdown(webhook, md[:4000])   # 截断到上限内
```

**几个必须注意的点：**

- **webhook 不进 git**：webhook URL 本身就是凭证，任何人拿到就能往群里发消息。放 `.env` 或环境变量，绝不写进提交的代码。
- **限流约 20 条/分钟**：批量推送时每条之间要 `sleep`（如 4 秒），否则会被限流。
- **长内容分条发**：单条 markdown 上限约 4096 字节，成果多时要拆成多条消息。

**若要在群里直接发文件**（而非只发链接），是另一套两步流程：先 `upload_media` 上传拿 `media_id`（3 天有效），再用 `media_id` 发 file 消息。但 PaperRAG 的主链路是"发链接"，因为 HTML 富文本更适合浏览器打开，且 COS 链接可长期访问（30 天）。

---

## ④ 成员下载 / 打开

成员在群里看到消息后：

- 点 **在线预览** → 浏览器直接打开 HTML 阅读（inline）。
- 点 **下载** → 浏览器强制下载 HTML 到本地，文件名友好，可永久保存。

因为预签名链接 30 天过期，**建议成员对想长期保留的成果点"下载"存档**，不要依赖群里的链接长期有效。

---

## 为什么这样设计（一句话总结每个决策）

| 决策 | 理由 |
|---|---|
| 成果存 COS、群里只发链接 | HTML 富文本不适合塞进企微；对象存储承载大文件更合适 |
| 私有桶 + 预签名 URL | 成果不公开可抓；靠临时签名链接受控访问 |
| 子账号 + 前缀权限 | 最小权限，密钥泄露影响面可控 |
| 一次上传、两个链接 | 省存储；预览"点开即读"、下载"永久存档"各取所需 |
| 下载链接覆盖 Content-Type | 不覆盖的话浏览器会无视 attachment 直接内联，下不下来 |
| markdown 而非卡片 | 群机器人 webhook 不支持 template_card |
| 所有密钥/webhook 走 .env | 凭证绝不进 git |

---

## 迁移到"另一个 bot"要改什么

这条链路是通用的，另一个 bot 复用时：

1. **成果生成部分换成你自己的**（本文档不涉及——你的 bot 生成什么 HTML 是你的事）。
2. **COS 部分几乎不用改**：只要把生成好的本地 HTML 路径 + 目标对象名传给 `upload_and_links()`，拿回两个链接。
3. **企微推送部分**：换 webhook（你自己群的）、换 markdown 内容模板（标题/介绍/链接排版）。
4. **配置**：在 `.env` 里配好 COS 密钥和 webhook，代码不动。

核心的两个函数 `upload_and_links(local_path, object_name)` 和 `send_markdown(webhook, content)` 是可以直接搬的——把"生成 HTML"和"组织 markdown 文案"换成你自己的即可。
