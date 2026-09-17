# slack-gif-creator（GIF）
## 用途
生成面向 Slack 优化的动画 GIF（大小/帧率/尺寸合适）。
## 触发
用户要做一个 GIF/动图给 Slack/聊天时使用。
## 核心做法
读 `assets/references/` 的说明与脚本；按目标限制调优 帧率/尺寸/颜色数；`python <脚本> --help` 后黑盒调用。
