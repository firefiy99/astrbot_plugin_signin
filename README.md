# AstrBot 积分签到插件

> 创作者：**小星萤**
> 完整的积分体系插件：每日签到、转账、排行榜（**转图片**）、补签、可配置奖池抽奖、**签到日历（图片）**、**积分商城（可自定义商品）**、完整的管理员指令。所有数据持久化到 SQLite，所有积分变动写流水可追溯。

## ✨ 功能特性

- 🎁 **每日签到**：基础积分 + 连续奖励 + 漏签宽限
- 💰 **积分查询**：查自己 / 查他人
- 📒 **积分流水**：完整记录每一笔变动，可分页
- 🏆 **排行榜图片化**：积分榜 + 签到榜用 `text_to_image` 渲染成图片
- 📆 **签到日历（图片）**：30 天格子视图，一目了然
- 💸 **积分转账**：用户间互转，可配手续费
- 🩹 **补签机制**：漏签用积分补回来
- 🎰 **可配置奖池抽奖**：WebUI 改 JSON 即生效
- 🛒 **积分商城**：商品可自定义，库存/兑换说明灵活配置
- 🛠 **完整管理**：
  - 加减分（带原因）
  - 修改任意用户的累计 / 连续 / 最长 / 上次签到日期
  - **清零指令**（全部 / 仅积分 / 仅签到 / 仅连续天数）
  - 商城商品增删改 + 重发兑换说明
- ⚙️ **22 个可配置项**，全部可视化
- 🔌 **零外部依赖**：仅 aiosqlite

## 📌 当前版本：v1.3.2

## 🎨 插件封面

AstrBot 4.5+ 会在插件市场显示一个 logo（256×256，1:1 PNG）。本仓库自带：

- **`astrbot_plugin_signin/logo.svg`** — 可缩放矢量图（推荐查看/编辑用）
- **`astrbot_plugin_signin/logo.png`** — 占位文件，可被 `generate_logo.py` 覆盖

**生成你的 logo.png（3 种方式任选）：**

### 方式 1：用仓库自带的 Python 脚本（推荐）
```bash
# 进入项目根目录（在 Termux 或任何 Python 环境）
python3 generate_logo.py
```
会生成一个橙红渐变圆角矩形 + 中心白色日历 + 7 格签到（3 绿 + 4 灰）的 logo.png。零依赖（只用 Python 标准库 zlib + struct）。

### 方式 2：直接用 SVG 转 PNG
```bash
# 用任何 SVG → PNG 工具，比如：
rsvg-convert -w 256 -h 256 astrbot_plugin_signin/logo.svg -o astrbot_plugin_signin/logo.png
# 或在线工具：https://svgtopng.com/
```

### 方式 3：自己画一个
任意 256×256 的 PNG，丢到 `astrbot_plugin_signin/logo.png` 即可。AstrBot 4.4 及以下没这功能，文件会被忽略，**不影响插件运行**。

> 创作者提示：也可以把 `logo.svg` 用 Inkscape / Figma / Adobe Illustrator 编辑（修改颜色、加文字"签到"、改成你的群风格）后再导出 PNG。

## 📦 安装

将整个 `astrbot_plugin_signin` 文件夹复制到 AstrBot 插件目录：

```
AstrBot/data/plugins/astrbot_plugin_signin/
```

然后在 AstrBot 的 WebUI 插件管理页点击「重载插件」即可。依赖会在首次加载时自动安装（若失败手动 `pip install aiosqlite`）。

## 🎮 指令列表

### 👤 用户指令（13 个）

| 指令 | 别名 | 说明 |
|---|---|---|
| `/签到` | qd, qiandao, daily | 每日签到 |
| `/积分` `[@人]` | jifen, points, 我的积分, 余额 | 查询积分 |
| `/积分记录` `[页码]` | 流水, jifenlog | 查询积分流水 |
| `/排行榜` | 排行, rank, top, 积分排行 | 积分榜（图片） |
| `/签到榜` | 签到排行, signrank | 签到天数榜（图片） |
| `/签到日历` `[@人]` | calendar, 我的签到, 日历 | 签到日历（图片，30 天） |
| `/转账` `@人 数量` | transfer, 给, 赠送 | 用户间转账 |
| `/补签` | 补卡, makeup | 补签（消耗积分） |
| `/抽奖` `[次数]` | lottery, 摇奖 | 抽奖，默认 1 次 |
| `/抽奖记录` `[页码]` | lottery_log, 摇奖记录 | 抽奖流水 |
| `/奖池` | lotterypool, 抽奖池 | 查看当前奖池 |
| `/商店` | shop, 商城, 积分商城 | 查看商城商品 |
| `/购买` `<ID>` | buy, 兑换 | 兑换商品 |
| `/我的兑换` `[页码]` | myorders, 兑换记录 | 我的兑换历史 |

### 🛠 管理员指令（9 个，需在 `admin_umo_list` 中配置白名单）

| 指令 | 用途 |
|---|---|
| `/addpoints @人 数量 原因` | 给用户增加正数积分 |
| `/reducepoints @人 数量 原因` | 扣除用户积分 |
| `/setsign @人 字段 值` | 设置签到数据 |
| `/清零 @人 [范围]` | 清零（all/points/signin/continuous） |
| `/商品上架 名字 \| 价格 \| 库存 \| 描述 \| 兑换` | 添加商品 |
| `/商品下架 <ID>` | 下架商品（软删） |
| `/商品改价 <ID> \| 新价 \| 新库存 \| 新说明` | 修改商品 |
| `/补发 <订单ID>` | 重发兑换说明 |

`/清零` 的别名：`reset` / `resetuser` / `重置`，兼容老指令名。

### `/setsign` 字段说明
```
/setsign @人 累计 30     - 设置累计签到
/setsign @人 连续 7      - 设置当前连续天数
/setsign @人 最长 10     - 设置历史最长连续
/setsign @人 上次 2024-01-01  - 设置上次签到日期
/setsign @人 上次 空     - 清空上次签到
/setsign @人             - 查看用户签到数据
```

### `/商品上架` 用法
```
/商品上架 名字 | 价格 | 库存 | 描述 | 兑换说明
例: /商品上架 群专属头衔 | 500 | -1 | 7天 | 联系群主
例: /商品上架 1000积分兑换券 | 800 | 10 | 立即到账 | 系统自动发放
```
- 价格必填
- 库存 `-1` 表示无限

### `/商品改价` 用法
```
/商品改价 <ID> | 新价格 | 新库存 | 新说明
例: /商品改价 1 | 300 | 20 | 限时优惠
```
- 价格/库存/说明 可选，传 空 表示不修改

## 💡 使用示例

```
/签到                       # 每日签到
/积分                       # 查自己
/积分 @张三                 # 查张三
/积分记录 2                 # 第 2 页流水
/排行榜                     # 积分榜（图片）
/签到榜                     # 签到榜（图片）
/签到日历                   # 签到日历（图片）
/签到日历 @张三             # 看他的签到日历
/转账 @李四 100             # 给李四转 100
/抽奖                       # 抽 1 次
/抽奖 10                    # 连抽 10 次
/奖池                       # 看奖池配置
/商店                       # 看商城商品
/购买 1                     # 兑换 ID=1 的商品
/我的兑换                   # 我的兑换历史
/补签                       # 补签昨天
```

管理员示例（假设你的 user_id = 123456，已加入白名单）：

```
/addpoints @王五 500 发奖金
/reducepoints @王五 50 违规
/setsign @张三                  # 查看张三的签到数据
/setsign @张三 累计 30
/setsign @张三 连续 7
/setsign @张三 上次 2024-01-15
/商品上架 群专属头衔 | 500 | -1 | 7天 | 联系群主
/商品下架 1
/商品改价 1 | 300 | 20 | 限时优惠
/补发 ORD20250101120000ABCD
/清零 @张三 all
/清零 @张三 points
/清零 @张三 signin
/清零 @张三 continuous
```

## ⚙️ 配置项（WebUI 直接改，共 23 个）

| 配置 | 默认 | 说明 |
|---|---|---|
| `base_points` | 10 | 每日签到基础积分 |
| `continuous_bonus_per_day` | 2 | 连续 N 天额外 +N×此值 |
| `continuous_bonus_max` | 30 | 连续奖励封顶 |
| `daily_sign_max_points` | 50 | 单次签到最多能拿到的积分 |
| `grace_hours` | 8 | 漏签宽限小时（凌晨 0~8 点可补算连续） |
| `transfer_min` | 1 | 单次转账最小积分 |
| `transfer_max` | 10000 | 单次转账最大积分 |
| `transfer_fee_rate` | 0.0 | 转账手续费率，0.05 = 5% |
| `makeup_cost` | 50 | 补签消耗积分，0 = 关闭 |
| `leaderboard_top_n` | 10 | 排行榜显示前 N 名 |
| `records_per_page` | 8 | 流水每页条数 |
| `admin_umo_list` | "" | 插件管理员白名单；推荐填 user_id（QQ 号），也支持完整 umo；可用中英文逗号、分号、空格或换行分隔，保存后需重载插件 |
| `welcome_message` | "欢迎 {nickname}..." | 首次签到欢迎语 |
| `lottery_enabled` | true | 是否启用抽奖 |
| `lottery_default_cost` | 10 | 每次抽奖消耗积分 |
| `lottery_daily_max_count` | 50 | 每人每天最多抽奖，0=不限 |
| `lottery_pool_json` | （奖池） | **奖池 JSON，可自由改** |
| `shop_enabled` | true | 是否启用积分商城 |
| `shop_default_items` | （默认商品） | **默认商品列表，可视化编辑** |
| `leaderboard_image_enabled` | true | 排行榜转图片（签到日历也用） |
| `signin_calendar_days` | 30 | 签到日历显示最近多少天 |
| `image_width` | 800 | 排行榜/日历图片宽度（像素） |

## 🎰 奖池配置（可自由改）

奖池在 WebUI 配置页的 `lottery_pool_json` 字段里直接编辑：

```json
[
  {"name": "🎉 谢谢参与", "weight": 50, "reward": 0},
  {"name": "💰 小额积分", "weight": 30, "reward": 50},
  {"name": "💎 大额积分", "weight": 15, "reward": 200},
  {"name": "🏆 暴富积分", "weight": 4, "reward": 1000},
  {"name": "✨ 欧皇积分", "weight": 1, "reward": 5000}
]
```

字段说明：
- `name`：奖品名称
- `weight`：权重，越大概率越高
- `reward`：抽中获得的积分

修改后**重载插件**生效。配置错误会自动回退到默认奖池，插件不会崩。

## 🛒 商城配置（可自由改）

### WebUI 配置（推荐）
在 WebUI 配置页的「默认商品列表」中直接编辑商品。第一次启动插件时，表为空时会自动导入这里的商品。后续修改 WebUI 不会影响已有商品（避免误删历史），需要用 `/商品上架` `/商品下架` `/商品改价` 指令管理。

格式：
```json
[
  {"name": "🎖️ 群专属头衔", "cost": 500, "description": "7 天专属头衔", "stock": -1, "delivery": "请联系群主开通"},
  {"name": "💎 1000 积分兑换券", "cost": 800, "description": "立即获得 1000 积分", "stock": 10, "delivery": "系统自动发放"},
  {"name": "🎁 神秘小礼物", "cost": 200, "description": "群主亲自送的小惊喜", "stock": -1, "delivery": "请联系群主领取"}
]
```

字段说明：
- `name`：商品名
- `cost`：价格（积分）
- `description`：描述
- `stock`：库存，`-1` = 无限
- `delivery`：兑换说明（自动发给用户）

### 管理员指令管理
- `/商品上架 名字 | 价格 | 库存 | 描述 | 兑换` — 添加
- `/商品下架 <ID>` — 软删（enabled=0）
- `/商品改价 <ID> | 新价 | 新库存 | 新说明` — 修改

### 购买流程
1. 用户 `/商店` 看商品列表（含 ID）
2. 用户 `/购买 <ID>` 兑换
3. 系统扣积分 + 扣库存（如果是限量）
4. 生成订单号 `ORD...`（带兑换说明）
5. 管理员可用 `/补发 <订单ID>` 重新发送兑换说明

## 🖼 排行榜转图片

`/排行榜` 和 `/签到榜` 默认会用 `html_render` 渲染成图片（在 AstrBot 的 WebUI 终端里很清晰），效果：
- 标题居中
- 🥇🥈🥉 奖牌
- 数据表格整齐排列

如果转图片失败（比如 WebUI 终端没启用 text_to_image），自动降级为文字版。

`leaderboard_image_enabled = false` 可以关闭图片模式。

## 📆 签到日历

`/签到日历` 或 `/签到日历 @某人`：
- 30 天格子视图（可配 `signin_calendar_days`）
- 5 个一行，每个 22px 表情
- ✅ 已签 🩹 补签 ⬜ 未签
- 今日有橙色边框
- 底部统计：已签/补签/连续/最高/签到率
- 同样用 `html_render` 渲染图片

## 🗄 数据存储

- **位置**：`data/astrbot_plugin_signin/signin.db`（在 AstrBot 工作目录的 `data/` 下）
- **表结构**（共 5 张表）：
  - `users` — 用户档案
  - `sign_log` — 签到记录
  - `points_log` — 积分变动流水（所有变动可追溯）
  - `lottery_log` — 抽奖记录
  - `shop_items` — 商城商品
  - `shop_purchases` — 兑换订单

**重要**：所有数据库文件都在 AstrBot 推荐的 `data/` 目录下，不会被插件更新覆盖。

### SQL 查询示例

```bash
sqlite3 data/astrbot_plugin_signin/signin.db

sqlite> SELECT * FROM users ORDER BY points DESC LIMIT 5;
sqlite> SELECT * FROM points_log WHERE user_id = '123456' ORDER BY id DESC LIMIT 10;
sqlite> SELECT * FROM shop_purchases ORDER BY id DESC LIMIT 10;
sqlite> .schema
```

## 📜 签到积分计算公式

```
bonus = min(连续天数 × continuous_bonus_per_day, continuous_bonus_max)
本次获得 = min(base_points + bonus, daily_sign_max_points)
```

默认配置下：连续 7 天签到，本次获得 = min(10 + 7×2, 50) = 24 积分。

## 🛠 常见问题

**Q: 改了配置不生效？**
A: 在 WebUI 插件管理页点击「重载插件」。奖池/商城 JSON 也需重载。

**Q: 数据库在哪里？**
A: `AstrBot/data/astrbot_plugin_signin/signin.db`

**Q: 怎么关掉某个功能？**
A:
- 关补签：`makeup_cost` 设为 0
- 关抽奖：`lottery_enabled` 设为 false
- 关商城：`shop_enabled` 设为 false
- 关图片：`leaderboard_image_enabled` 设为 false（自动降级文字）

**Q: 怎么把我自己设为管理员？**
A: WebUI 插件配置页，`admin_umo_list` 填你的 user_id（不知道的话先在群里发消息，看 AstrBot 日志拿到你的 user_id），多个用英文逗号分隔。

**Q: 抽奖 JSON 写错了怎么办？**
A: 不用担心，插件会自动回退到默认奖池，并在 AstrBot 日志里打印警告。机器人不会崩。

**Q: 商城商品会被 WebUI 配置改掉吗？**
A: 不会。「默认商品列表」只在**首次启动**（商品表为空）时导入一次。后续要改商品，请用 `/商品上架/下架/改价` 指令。

**Q: 怎么把排行榜改成不发图片？**
A: WebUI 把 `leaderboard_image_enabled` 设为 false，会自动改回文字版。

## 📄 许可

MIT — by **小星萤**
