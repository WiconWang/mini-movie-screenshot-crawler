# 视频下载与归档规范

## 1. 目录结构

```
<仓库根>/
├── .cache/                       # 运行时缓存（gitignore）
│   ├── cookies.txt               #   Netscape 格式登录态（仅 bilibili 域，0600）
│   └── index/
│       └── BV1Zp4y187oL.json     #   分P索引缓存
└── downloads/                    # 下载产出（gitignore）
    └── {game}/                   # 游戏分类目录：genshin / 后续新游戏…
        └── {series}/             # 剧集目录：--series 指定，默认=视频标题（清洗后）
            ├── 157-盛夏！海岛？大冒险！ 其一 ….mp4
            ├── 157-….mp4.meta.json
            └── …
```

- `game`：来自 SKILL.md 游戏路由表；`series` 未指定时用视频标题自动清洗生成
- 同一分P重复下载默认跳过；`--force` 覆盖

## 2. 文件命名

`{分P号:03d}-{分P标题}.mp4`

- 分P号三位零填充：同一源视频内稳定唯一，天然保持剧情顺序
- 标题清洗规则（跨平台安全）：
  - 删除 ASCII 非法字符 `\ / : * ? " < > |` 与控制符
  - 中文全角标点（！？「」…）保留原样
  - 连续空白合并为单空格；首尾空格与点去除；总长截断至 80 字符

## 3. 索引缓存格式（.cache/index/<BV号>.json）

```json
{
 "bvid": "BV1Zp4y187oL",
 "title": "开局捡到应急食品，然后天下无敌",
 "url": "https://www.bilibili.com/video/BV1Zp4y187oL",
 "fetched_at": 1755900000.0,
 "fetched_via": "view API",
 "page_count": 200,
 "pages": [{"p": 157, "title": "盛夏！海岛？大冒险！ 其一 …", "duration": 3156}]
}
```

- 抓取顺序：优先 bilibili view API，失败时兜底 `yt-dlp --flat-playlist -J`
- 新鲜期 7 天（`INDEX_MAX_AGE`）；cookies.txt 复用期 6 小时（`COOKIES_MAX_AGE`）

## 4. meta.json 字段

每个视频附带同名 `<文件名>.meta.json`：

```json
{
 "source_url": "https://www.bilibili.com/video/BV1Zp4y187oL?p=157",
 "bvid": "BV1Zp4y187oL",
 "page": 157,
 "page_title": "盛夏！海岛？大冒险！ 其一 迷境之岛！无法预测的旅行",
 "video_title": "开局捡到应急食品，然后天下无敌",
 "duration_sec": 3156,
 "resolution": "1920x1080",
 "size_bytes": 673741824,
 "downloaded_at": "2026-08-23T12:00:00+08:00"
}
```

## 5. 清晰度选择策略

yt-dlp 参数：`-f "bv*+ba/b" -S "res,vcodec:avc1,acodec:aac,br" --merge-output-format mp4`

| 优先级 | 维度 | 含义 |
|--------|------|------|
| 1 | `res` | 分辨率最高优先（账号权限内的最高清） |
| 2 | `vcodec:avc1` | 同分辨率下优先 h264 —— 符合物料规范 mp4(h264/h265) |
| 3 | `acodec:aac` | 音轨优先 aac |
| 4 | `br` | 同规格取高码率 |

实测基准实录（大会员账号）：最高为 1920x1080@60fps avc1；该源未提供 hevc/av01/4K。

## 6. 物料规范衔接

产出 mp4 满足 mini-movie-maker 物料规范 §2（mp4/h264、≥1080p、含原始音轨）。
登记台账时以 `meta.json` 的 source_url/page_title 回填 version/chapter 信息。
