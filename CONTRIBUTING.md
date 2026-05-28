# Contributing

欢迎提交模板适配、识别稳定性和文档改进。

## 本地开发

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -r requirements.txt
python -m compileall .\grab_hongbao.py
python .\grab_hongbao.py --help
```

调试识别时优先使用：

```powershell
python .\grab_hongbao.py --dry-run --scan-once --verbose --debug-dir .\debug
```

## 提交建议

- 不要提交自己的 `config.json`。
- 不要提交包含头像、昵称、聊天内容的截图。
- 如果修改模板匹配逻辑，请说明测试环境、微信版本、Windows 缩放比例和阈值。
- 不接受绕过平台限制、批量骚扰、账号规避或对抗检测相关改动。
