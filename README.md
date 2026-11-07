# product-scout

**给内容创作者的选题池。** 每天从 GitHub、Hugging Face、Hacker News 发现正在变火的产品和工具，用大模型判断“值不值得拍成视频”，再看看中文圈有没有人已经讲过，最后按优先级排好队等你挑。

[English](#english)

## 它和“热点新闻站”有什么不同

新闻是流水，过了三天就没用了；选题是库存，今天没做完，明天还能接着做。所以 product-scout 不会每天给你一份新清单，而是维护一个**会自己重新排序的选题池**：

- **视频价值**（0–100）：画面冲击力、能不能上手、是不是平替、观众相关度、新鲜感。由大模型独立打两次分，结果分歧大时再打第三次。这个分数基本不变。
- **时效**（0–1）：star、like、HN 分数的增速，在几个平台同时出现，产品有多新；**中文圈已经有人讲过，时效就打折**。每天重新计算，会逐渐衰减。
- **优先级** = 视频价值 × (0.35 + 0.65 × 时效)。价值高但不火的产品不会被挤掉，会一直留在池子里当“常青选题”。
- **窗口期提醒**：价值高、正在火、中文圈还没人讲。这样的选题最好这几天就做。大多数天这里是空的，这很正常。

每个产品都有状态：`新发现 → 观察中 → 已试用 → 排期 → 已发布`，任何一步都可以标记为 `放弃`。

## 快速开始

需要 Python 3.10 或更高版本。

```bash
git clone <这个仓库> && cd product-scout
pip install -e .
```

建一个目录放你自己的配置和数据：

```bash
product-scout init my-scout
cd my-scout
cp .env.example .env   # 填上大模型 Key
product-scout run      # 抓取 → 打分 → 中文覆盖度 → 排序 → 日报
product-scout serve    # 打开 http://127.0.0.1:8765 管理选题池
```

如果命令行提示找不到 `product-scout` 命令，可以用 `python -m product_scout` 代替。

没有大模型 Key 也能跑，只是没有视频价值分，只按热度排序。

### 大模型

任何 OpenAI 兼容接口都可以，在 `product-scout.toml` 的 `[llm]` 里配置：

| 服务 | base_url | model |
|---|---|---|
| DeepSeek（默认） | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| 智谱 | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| 本地 Ollama | `http://localhost:11434/v1` | 你的模型名（Key 随便填） |
| Claude | `https://api.anthropic.com/v1` | `claude-sonnet-5-5` |

Key 放在 `.env` 里的 `SCOUT_LLM_API_KEY`。每个新产品调用 2–3 次，每次运行最多处理 `max_new_per_run` 个（默认 40）。

用 Claude 时会自动改走 Anthropic 官方 SDK（需要先 `pip install -e ".[anthropic]"`），并用结构化输出保证返回的是合法 JSON。Claude 不接受自定义 `temperature`；思考强度用 `effort` 设置（默认 `low`，打分这类任务够用）。

### 网络

访问 GitHub、HF、HN 需要能连外网。在 `[network] proxy` 填代理地址，或者设置环境变量 `HTTPS_PROXY`。B 站覆盖度检查默认**不走代理**（`[coverage] use_proxy = false`）。

## 命令

| 命令 | 作用 |
|---|---|
| `product-scout run` | 每日流程，生成 `data/reports/日期.md` |
| `product-scout serve` | 本地网页：窗口期 / 选题池 / 各状态，改状态、置顶、写备注 |
| `product-scout list [--status window]` | 在终端看选题池 |
| `product-scout status <id> tried` | 在终端改状态 |
| `product-scout fetch / score / coverage / rank` | 单独跑某一步 |
| `product-scout export --format md\|rss\|json` | 导出，方便接入其他工具 |

### 每天自动运行

macOS / Linux（cron，每天 8 点）：

```bash
0 8 * * * cd /path/to/my-scout && product-scout run >> data/run.log 2>&1
```

Windows（任务计划程序）：

```powershell
$dir = "D:\my-scout"; $pyw = (Get-Command pythonw).Source
Register-ScheduledTask -TaskName "product-scout daily run" `
  -Trigger (New-ScheduledTaskTrigger -Daily -At "08:00") `
  -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable) `
  -Action (New-ScheduledTaskAction -Execute $pyw -WorkingDirectory $dir -Argument "-m product_scout --log data\run.log run")
```

用 `pythonw` 加 `--log` 运行时不会弹出命令行窗口，输出会追加写入日志文件。`-StartWhenAvailable` 表示到点时电脑没开的话，开机后会补跑。想让网页一直开着，可以用同样的方法再注册一个开机启动的任务（`New-ScheduledTaskTrigger -AtLogOn`），参数换成 `--log data\serve.log serve`。

## 改成你自己的口味

最值得花时间的是 **`prompts/value-score.md`**：它定义了你的观众是谁、什么样的产品值得拍。做 AI 工具号、设计工具号、硬件号的人，标准完全不同，把你的选题经验写进去就行。改完直接生效，不用改代码。

`product-scout.toml` 里还能调这些：

- `[scoring.weights]`：五个维度的权重
- `[scoring.risk_penalty]`：灰产、版权、NSFW 等风险的扣分
- `[sources.*]`：每个信源的门槛，例如 GitHub 加上 `query_extra = "topic:ai"`
- `[rank]`：窗口期的门槛

## 信源

| 信源 | 默认 | 需要什么 |
|---|---|---|
| GitHub：最近新建、star 涨得快的仓库 | 开 | 可选 `GITHUB_TOKEN`，提高接口限额 |
| Hugging Face：Trending Spaces | 开 | 无 |
| Hacker News：Show HN，以及链接到 GitHub 仓库的热门帖子 | 开 | 无 |

同一个产品出现在多个平台时，会按链接自动合并成一条。

**添加信源**：在 `src/product_scout/sources/` 新建一个模块，提供 `NAME` 和 `fetch(http, cfg) -> list[Item]`，再登记到 `sources/__init__.py` 的 `REGISTRY`。欢迎提交 Reddit、Product Hunt、X、V2EX、即刻等信源。

**中文圈覆盖度**目前查的是 B 站视频搜索：标题里出现了产品名、并且发布在产品出现之后的视频才算数。想检查其他平台或其他语种社区，在 `src/product_scout/coverage/` 加一个提供方即可。

## 已知局限

- 产品名是常见单词（比如 “player”）时，覆盖度检查可能误判。
- HN 帖子的发布时间被当作产品的出现时间。
- B 站搜索接口是公开的网页接口，以后可能变化。

## 开发

```bash
pip install -e .
python -m unittest discover -s tests -v
```

整个项目只依赖 Python 标准库，外加 `certifi`（某些 Python 自带的证书库太旧，验证不了 api.github.com 的证书链）。3.10 版本还需要 `tomli`。

## 许可

MIT

---

## English

**A topic pool for content creators.** product-scout finds products and tools that are taking off on GitHub, Hugging Face and Hacker News, asks an LLM how well each would work as a video, checks whether creators in your community have already covered it, and keeps everything in a pool that re-ranks itself every day.

- **Value** (0–100): visual impact, how easy it is to try, whether it replaces a paid product, audience fit, novelty. Scored twice by an LLM, independently, with a third opinion when the two disagree.
- **Timeliness** (0–1): growth rate, cross-platform spread and freshness. It drops once others have covered the product.
- **Priority** = value × (0.35 + 0.65 × timeliness), so strong evergreen picks stay in the pool.
- **Window alerts**: high value, rising fast, not covered yet.

```bash
pip install -e .
product-scout init my-scout && cd my-scout
cp .env.example .env      # add your LLM key (any OpenAI-compatible API)
product-scout run
product-scout serve       # http://127.0.0.1:8765
```

Edit `prompts/value-score.md` to describe your audience and what makes a good video for you. The default prompt targets Chinese short-video audiences; the coverage check uses Bilibili. Both are pluggable.

License: MIT.
