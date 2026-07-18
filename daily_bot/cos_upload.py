#!/usr/bin/env python3
"""
cos_upload —— 把生成的 HTML 上传到腾讯云 COS（私有桶），一次上传生成【两个】30 天预签名链接：
  预览(preview)：response-content-disposition=inline → 浏览器直接打开阅读；
  下载(download)：response-content-disposition=attachment; filename=... → 强制下载，文件名友好。
两个链接指向同一个对象，只上传一次（COS 支持在预签名 URL 里覆盖响应头）。

为何要下载：预签名 URL 30 天过期；下载让成员把 HTML 存到本地永久保存，不依赖会过期的链接。
为何也给预览：方便在群里点开即读，不必先下载。

配置从环境变量读取（daily_bot/.env，run.py 启动时加载）：
  COS_SECRET_ID / COS_SECRET_KEY、COS_REGION、COS_BUCKET、COS_PREFIX。
子账号 qs-paperrag 仅可读写 COS_PREFIX（paper_rag/）下的对象。
"""

import os

from qcloud_cos import CosConfig, CosS3Client


def _cfg():
    sid = os.environ.get("COS_SECRET_ID")
    skey = os.environ.get("COS_SECRET_KEY")
    region = os.environ.get("COS_REGION", "ap-hongkong")
    bucket = os.environ.get("COS_BUCKET")
    prefix = os.environ.get("COS_PREFIX", "paper_rag/")
    if not (sid and skey and bucket):
        raise RuntimeError("COS 配置缺失：需在 daily_bot/.env 设置 "
                           "COS_SECRET_ID / COS_SECRET_KEY / COS_BUCKET")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return sid, skey, region, bucket, prefix


def _upload(local_path, object_name):
    """上传到 paper_rag/{object_name}，存为 text/html（便于预览）。返回 (client, bucket, key)。"""
    sid, skey, region, bucket, prefix = _cfg()
    client = CosS3Client(CosConfig(Region=region, SecretId=sid, SecretKey=skey))
    key = prefix + object_name
    with open(local_path, "rb") as f:
        body = f.read()
    client.put_object(Bucket=bucket, Key=key, Body=body,
                      ContentType="text/html; charset=utf-8")
    return client, bucket, key


def _sign(client, bucket, key, expires, disposition, content_type=None):
    """生成预签名 GET URL；用 response-content-disposition 覆盖处置头，可选覆盖 response-content-type。"""
    params = {"response-content-disposition": disposition}
    if content_type:
        params["response-content-type"] = content_type
    return client.get_presigned_url(
        Method="GET", Bucket=bucket, Key=key, Expired=expires, Params=params)


def upload_and_links(local_path, object_name, expires=2592000):
    """
    上传一次，返回 (preview_url, download_url)：
      preview  → inline（Content-Type 保持 text/html → 浏览器直接打开阅读）
      download → attachment; filename="{object_name}" + response-content-type=application/octet-stream
                 （必须同时覆盖 Content-Type，否则浏览器仍把 text/html 内联渲染、忽略 attachment）
    默认 30 天有效。任何失败抛异常，由调用方处理。
    """
    client, bucket, key = _upload(local_path, object_name)
    # 预览链接：保持不变（只 inline，Content-Type 仍是 text/html，浏览器内联显示）
    preview = _sign(client, bucket, key, expires, "inline")
    # 下载链接：attachment + 把 Content-Type 改成不可渲染类型，浏览器才会强制下载
    download = _sign(client, bucket, key, expires,
                     f'attachment; filename="{object_name}"',
                     content_type="application/octet-stream")
    return preview, download
