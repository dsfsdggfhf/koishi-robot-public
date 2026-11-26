# Koishi Robot · 一只恋妖精

面向 B 站的社交机器人。公开版包含本机社交机器人、云端轻量服务、手机控制台源码和示例配置。

- 本机支持按规则查找视频、生成评论或推荐草稿，以及可选的字幕、画面和音频分析。
- 云端支持私信和表情、早晚安、节日节气、生日提醒和动态通知，包含退订、去重、投递回执及恢复保护。
- 手机控制台源码包含本地控制、配对和只读会话。可选的界面美术资源可自行添加。

## 本机预演

需要 Python 3.12 或更新版本。从仓库根目录运行：

    python -m pip install -r requirements.txt
    python main.py init
    python main.py --help

检查生成的 config.json，配置你有权使用的 B 站账号和接收对象。示例默认 dry_run 为 true。可选的媒体分析需要另装 FFmpeg 等工具。

## 云端入口和离线测试

    python -m pip install -r cloud_social/requirements.txt
    python -m cloud_social --help
    python -m unittest discover -s cloud_social -t . -q

云端从部署者的私有目录读取配置、凭据、初始状态和 SQLite。账号 Cookie、API 密钥、接收名单、用户聊天与本机运行日志按私人数据管理。

本仓库发布社交机器人源码、测试和必要的示例模板。独立的弹幕训练项目、私有运维工具和自用美术素材在发布范围之外。
