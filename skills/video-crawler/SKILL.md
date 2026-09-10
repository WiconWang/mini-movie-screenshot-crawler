---
name: game-storyline-video-crawler
description: >
  在 bilibili 检索并下载游戏剧情实录视频：分P索引本地缓存、关键词快速查找，
  用户确认后用 yt-dlp（Firefox 登录态）下载最高清晰度原盘，按游戏分类目录归档。
  当前已收录游戏：原神（基准实录 BV1Zp4y187oL）。
  触发词：下载视频、剧情实录、bilibili下载、BV号、分P下载、盛夏海岛大冒险、
  最高清晰度下载、video-crawler。
---

# 视频采集器

在 bilibili 查找游戏剧情实录视频，经用户确认后下载最高清晰度原盘并分类归档。

## 游戏路由表

根据用户诉求判断所属游戏，确定分类目录与基准实录：

| 游戏 | 分类目录（--game） | 基准实录 | 状态 |
|------|-------------------|----------|------|
| 原神 | `genshin` | `BV1Zp4y187oL`（《开局捡到应急食品，然后天下无敌》全流程实录） | ✅ 已收录 |

> 新增游戏时在此表添加一行即可；无法判断游戏归属时先询问用户。

## 前置条件（下载前必须预检）

运行 `check` 模式确认以下三项，任何 FAIL 先解决再继续：

1. **yt-dlp** 已安装
2. **ffmpeg** 已安装（合并音视频必需）
3. **Firefox 已登录 bilibili** —— 脚本自动从 Firefox profile 的 `cookies.sqlite`
   导出登录态（含 SESSDATA），用于解锁高清晰度版本：
   - WSL 环境：自动探测 `/mnt/c/Users/*/AppData/Roaming/Mozilla/Firefox/Profiles/*/cookies.sqlite`
   - 原生 Linux：自动探测 `~/.mozilla/firefox/*/cookies.sqlite`
   - 特殊路径可用环境变量 `FIREFOX_PROFILE_DIR` 指定

## 四种模式

```bash
# 环境自检
python3 scripts/bilibili_video_crawler.py check

# 抓取分P索引并缓存（7 天内重复执行直接命中缓存）
python3 scripts/bilibili_video_crawler.py index "<URL或BV号>" [--refresh]

# 在缓存索引中查找（多词 AND 匹配，纯本地无网络请求）
python3 scripts/bilibili_video_crawler.py search 盛夏 海岛 [--json]

# 下载指定分P最高清晰度，自动归档命名
# 独立使用：-o downloads；集成 game-storyline-pipeline 管线时先落暂存，再逐分P登记：
python3 scripts/bilibili_video_crawler.py download BV1Zp4y187oL -p 157 158 \
    --game genshin -o /tmp/video-staging --series "1.6-盛夏！海岛？大冒险！"
# 落盘：/tmp/video-staging/genshin/{--series}/{分P}-{标题}.mp4（+ 同名 .mp4.meta.json）
# 登记进统一台账（seg = quest 内剧情序号，非 bilibili 分P号；meta 的 bvid/page/page_title/duration/resolution 进 ledger meta_json，伴生文件不入库）：
mmm add-asset --game genshin --version 1.6 --slug <quest_slug> \
    --kind video --seg <N> --src "/tmp/video-staging/genshin/1.6-盛夏！海岛？大冒险！/157-….mp4" \
    --source-url "https://www.bilibili.com/video/BV1Zp4y187oL?p=157"
```

`download` 可选项：

- `--max-height N`：限制最大分辨率（仅调试用，默认取账号可用最高清）
- `--force`：覆盖已存在文件（默认跳过）
- `--fresh-cookies`：强制重新导出 cookies（登录态变更后使用）

## 典型工作流

1. **解析诉求**：从用户描述中提取剧名/集名，按游戏路由表确定 `--game` 与基准实录；
   用户给了参考 URL 则直接以其 BV 号为准
2. **索引查找**：`search <关键词>` 查本地缓存 →
   命中：列出候选（分P号 + 标题 + 时长），**必须经用户确认后才可下载**
3. **未命中**：`index <基准URL> --refresh` 刷新后再搜 →
   仍未命中：请用户提供具体 URL，不要自行猜测分P号
4. **预检**：用户确认后运行 `check`，FAIL 项解决前不得下载
5. **下载**：`download` 所选分P（默认最高清晰度），脚本自动清洗命名并写 meta
6. **报告 + 登记**：向用户报告下载清单——文件路径、大小、分辨率、来源 URL；管线集成时按上节 `mmm add-asset --kind video` 逐分P登记（`--seg` 由用户按剧情顺序确认）

## 索引缓存机制

- 位置：`.cache/index/<BV号>.json`，记录全部 200 个分P的标题与时长
- 新鲜期 7 天，过期后下次 `index` 自动重抓；`search` 只读缓存不联网
- 缓存损坏时删除对应 JSON 重跑 `index` 即可

## 存储与命名规范

详见 [references/download-spec.md](references/download-spec.md)。核心结构（暂存形态，登记时 `add-asset` 复制入库并改名 `p{NNN}.mp4`）：

```
{out_dir}/                            # 独立使用默认 downloads/；管线集成用 /tmp 暂存
└── genshin/                          # --game 分类目录（与统一台账 game code 一致）
    └── 1.6-盛夏！海岛？大冒险！        # --series 剧集目录（暂存标签，不进台账身份）
        ├── 157-盛夏！海岛？大冒险！ 其一 ….mp4      # {分P序号}-{分P标题}
        ├── 157-….mp4.meta.json                    # 来源与分辨率元数据（登记时并入 ledger，不过盘）
        └── …
```

## 注意事项

- `.cache/cookies.txt` 含账号登录态：已 gitignore，勿提交、勿外传，用完可删
- 最高清晰度受账号权限限制（当前账号为大会员）；源视频本身未提供的分辨率无法获得
- 视频体积大（单集约 0.3~1.6GB）：批量下载前向用户确认磁盘空间（`check` 会显示剩余空间）
- 运行环境为 WSL/Linux，脚本为纯 Python 标准库实现，无第三方依赖
