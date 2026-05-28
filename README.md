# PC 微信红包 CV 自动助手

过年闲来无事，简单做了一个基于 CV 视觉识别的 PC 端微信自动抢红包助手。

它通过 OpenCV 模板匹配识别 Windows 桌面微信里的红包卡片、打开按钮和关闭按钮，再用 PyAutoGUI 完成点击。项目保持单文件主程序，方便看懂、修改和本地运行。

> 免责声明：本项目仅用于学习 Python 桌面自动化和 OpenCV 模板匹配。使用自动化工具可能违反微信或相关平台的服务条款，也可能造成账号风险。请只在你拥有明确授权的本机环境中测试，不要用于骚扰、刷屏、牟利或影响他人的场景。

## 功能

- 基于 OpenCV 的红包卡片、开按钮、关闭按钮模板匹配
- 支持自定义截图模板，适配不同分辨率、缩放比例和微信版本
- 支持命令行参数和 JSON 配置文件
- 支持 `--dry-run` 只检测不点击，方便调试模板
- 支持 `--once`、`--scan-once`、`--max-opens` 控制运行边界
- 支持 `--region left,top,width,height` 限定扫描区域，降低误识别和 CPU 占用
- 支持 `--debug-dir` 保存匹配截图，便于排查识别问题
- 保留 PyAutoGUI failsafe：鼠标移到屏幕左上角可中断自动化

## 环境要求

- Windows 10/11
- PC 版微信
- Python 3.10 或更高版本

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -r requirements.txt
```

## 准备模板图

模板图必须来自你自己的电脑截图，因为微信版本、字体渲染、DPI 缩放、显示器分辨率都会影响识别效果。

当前仓库里提供了三个示例模板：

- `hongbao_icon.png`：聊天窗口里的红包卡片
- `open_button.png`：弹窗里的“开”按钮
- `close_button.png`：结果页关闭按钮

如果识别不稳定，建议重新截图覆盖这些文件：

1. 打开 PC 微信。
2. 截取聊天窗口里的红包卡片区域，保存为 `hongbao_icon.png`。
3. 点击红包后，截取“开”按钮，保存为 `open_button.png`。
4. 打开后截取关闭按钮，保存为 `close_button.png`。

截图区域越干净越好，不要包含头像、昵称、聊天背景或其他会变化的信息。

## 快速运行

先做一次只检测不点击的检查：

```powershell
python .\grab_hongbao.py --dry-run --scan-once --verbose --debug-dir .\debug
```

开始监听：

```powershell
python .\grab_hongbao.py
```

只处理一次后退出：

```powershell
python .\grab_hongbao.py --once
```

最多处理 3 次后退出：

```powershell
python .\grab_hongbao.py --max-opens 3
```

只扫描微信窗口所在区域：

```powershell
python .\grab_hongbao.py --region 120,80,900,760
```

## 配置文件

复制示例配置：

```powershell
Copy-Item .\config.example.json .\config.json
```

运行：

```powershell
python .\grab_hongbao.py --config .\config.json
```

命令行参数会覆盖配置文件里的同名设置。

## 常用参数

| 参数 | 说明 |
| --- | --- |
| `--threshold 0.8` | 模板匹配置信度，越高越严格 |
| `--region 120,80,900,760` | 限定扫描区域：左、上、宽、高 |
| `--dry-run` | 只识别不点击 |
| `--scan-once` | 扫描一次就退出 |
| `--once` | 成功处理一次红包流程后退出 |
| `--max-opens 3` | 达到指定次数后退出 |
| `--debug-dir .\debug` | 保存匹配到的截图 |
| `--verbose` | 输出调试日志和匹配分数 |

## 排查

识别不到：

- 重新截取模板图，确认屏幕缩放没有变化。
- 降低阈值，例如 `--threshold 0.72`。
- 使用 `--debug-dir` 查看匹配截图。
- 用 `--region` 缩小扫描范围。

误点击：

- 提高阈值，例如 `--threshold 0.9`。
- 重新截取更有辨识度的模板，不要包含背景。
- 使用 `--dry-run --verbose` 先观察日志。

鼠标不动或点击无效：

- 确认终端和微信不被安全软件拦截。
- 必要时使用管理员终端运行。
- 将鼠标移动到屏幕左上角可触发 PyAutoGUI failsafe。

## 发布流程

完整 GitHub 发布步骤见 [docs/PUBLISHING.md](docs/PUBLISHING.md)。

## 许可证

MIT License，详见 [LICENSE](LICENSE)。
