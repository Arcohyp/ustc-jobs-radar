# USTC Jobs Radar

中科大就业网**专场招聘会**自动追踪工具：每天抓取宣讲会列表，按你的求职画像（`profile.yaml`）分档匹配，生成日历图，并可通过钉钉机器人推送每日摘要。

## 功能

- **增量抓取**：就业网公开 JSON 接口，串行请求 + 礼貌延迟（1.5~2.5s/次）
- **自动清理**：剔除举办日期早于今天的场次
- **详情正文缓存**：`data/details.jsonl` 覆盖全部未开始场次，仅对新场次增量补抓（首次全量约几分钟，之后每天几次请求）
- **画像匹配**（纯关键词规则，无 LLM）：
  - 🎯 目标企业：标题命中目标清单（含别名）
  - ⚠️ 央国企标记：标题命中信号词或国企名录（`soe_names.txt`，可选）则单独分档提醒（支持例外名单）
  - 🔎 值得一看：方向高度相关的非清单企业，附关联理由
  - 📄 JD 正文命中：标题未命中但详情正文命中 ≥2 个强关键词（防漏网，如"岚图汽车"标题无方向词但 JD 里有 VLA/世界模型）
  - 🔍 方向相关：标题命中 ≥2 个方向关键词兜底
- **日历图**：双月日历视图，按 A/B/C 优先级着色，标注撞车取舍
- **钉钉日报**：每日推送新增匹配 + 分档摘要（复用 [dingtalk-server-monitor](https://github.com/Arcohyp/dingtalk-server-monitor) 的 notifier）

## 快速开始

```bash
pip install -r requirements.txt
cp profile.example.yaml profile.yaml   # 然后按自己的求职意向修改
python3 collect.py                     # 全量更新 + 详情补抓 + 匹配
python3 collect.py --no-details        # 跳过详情抓取，只按标题匹配（快）
python3 collect.py --offline           # 不联网，用本地存储重新生成报告（调规则用）
python3 render_schedule.py             # 生成 data/schedule.png 日历图
```

## 每日自动更新 + 钉钉推送

```bash
# 30 8 * * * = 每天 08:30
crontab -e
```

```cron
30 8 * * * DSM_ROOT=/path/to/dingtalk-server-monitor /usr/bin/python3 /path/to/ustc-jobs-radar/daily_update.py >> /path/to/ustc-jobs-radar/logs/cron.log 2>&1
```

钉钉机器人 webhook 配置见 dingtalk-server-monitor 的 README（`DINGTALK_WEBHOOK` / `DINGTALK_SECRET` 环境变量或其 `config/secrets.yaml`）。抓取失败也会推送错误通知，不会静默。

调试：

```bash
DSM_ROOT=... python3 daily_update.py --dry-run    # 只打印摘要，不联网、不发送
DSM_ROOT=... python3 daily_update.py --no-fetch   # 用本地报告生成摘要并发送
```

## 日历图中文字体

matplotlib 只注册 `.ttc` 合集的第一个字体（JP），需提取 SC 字体到 `fonts/`：

```bash
python3 -c "
from fontTools.ttLib import TTCollection
import os
os.makedirs('fonts', exist_ok=True)
for src, out in [('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc', 'NotoSansCJKsc-Regular.otf'),
                 ('/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc', 'NotoSansCJKsc-Bold.otf')]:
    for f in TTCollection(src).fonts:
        if f['name'].getDebugName(1) == 'Noto Sans CJK SC':
            f.save('fonts/' + out)
            break
"
```

## 国企名录（可选）

如果你有地方国企名录，把公司名一行一个存为项目根目录的 `soe_names.txt` 即自动生效（标题含名录内公司名或其短名即标记）。该文件已在 `.gitignore` 中排除，**请勿将名录本身提交或外传**。

## 合规说明

- 仅访问就业网前端自己调用的**公开** JSON 接口，不登录、不绕过验证码或权限控制
- 串行请求 + 礼貌延迟，建议每天运行一次，不要高频轮询
- 抓取数据仅用于个人求职参考

## License

MIT（`fonts/` 下的 Noto Sans CJK 字体文件为 SIL OFL 许可，由使用者自行从系统字体提取，不在本仓库分发）
