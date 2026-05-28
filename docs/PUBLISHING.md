# GitHub 发布流程

## 1. 本地检查

```powershell
python -m pip install -r requirements.txt
python -m compileall .\grab_hongbao.py
python .\grab_hongbao.py --help
python .\grab_hongbao.py --dry-run --scan-once --verbose
```

最后一条命令需要当前屏幕环境可截图。如果没有打开微信或没有红包模板命中，退出码为 1 是正常的；重点看程序是否能正常启动和输出日志。

## 2. 初始化 Git

```powershell
git init -b main
git add .
git commit -m "Initial release"
```

## 3. 用 GitHub CLI 发布

安装 GitHub CLI：

```powershell
winget install --id GitHub.cli -e
```

登录：

```powershell
gh auth login
```

创建公开仓库并推送：

```powershell
gh repo create wechat-red-packet-cv-helper --public --source . --remote origin --push --description "过年闲来无事做的基于 CV 视觉识别的 PC 端微信自动抢红包助手"
```

如果想先建私有仓库，把 `--public` 改成 `--private`。

## 4. 不用 GitHub CLI 的发布方式

1. 在 GitHub 网页新建仓库，例如 `wechat-red-packet-cv-helper`。
2. 不要勾选自动创建 README、LICENSE 或 `.gitignore`，因为本地已经有这些文件。
3. 复制 GitHub 给出的远端地址后执行：

```powershell
git remote add origin https://github.com/<你的用户名>/wechat-red-packet-cv-helper.git
git branch -M main
git push -u origin main
```

## 5. 打标签发布版本

```powershell
git tag -a v0.1.0 -m "v0.1.0"
git push origin v0.1.0
```

然后在 GitHub Releases 页面基于 `v0.1.0` 创建 Release，说明可以直接使用 `CHANGELOG.md` 里的内容。

## 6. 推荐仓库设置

- About 描述：`过年闲来无事做的基于 CV 视觉识别的 PC 端微信自动抢红包助手`
- Topics：`python`, `opencv`, `pyautogui`, `wechat`, `automation`, `computer-vision`
- 勾选 Issues，方便收集模板识别问题
- 如果只是学习项目，建议先保持公开仓库但不要发布打包后的可执行文件
