# AKI 星星量筛选器使用步骤

## 1. 这套文件放在哪里

我已经把新版本放在你的桌面：

`/Users/jiangyuehan/Desktop/AKI星星量股票筛选器`

这一套是 AKI 独立版本，和你 GitHub 原来正在运行的文件分开。

## 2. 要上传到 GitHub 的文件

打开桌面上的 `AKI星星量股票筛选器` 文件夹，把里面这些文件上传到你现有的 GitHub 仓库：

- `AKI_star_volume_scanner.py`
- `AKI_requirements.txt`
- `AKI_config.example.json`
- `AKI_README.md`
- `.github/workflows/AKI_star_volume.yml`
- `tests/test_AKI_star_volume_scanner.py`

注意：`.github` 是隐藏文件夹，如果 Finder 看不到，可以在 Finder 里按 `Command + Shift + .` 显示隐藏文件。

## 3. 在 GitHub 网页上传文件

1. 打开你的仓库：`https://github.com/Johnaki/stock-screener`
2. 点绿色 `Code` 按钮左边附近的 `Add file`
3. 选择 `Upload files`
4. 把桌面 `AKI星星量股票筛选器` 里的文件拖进去
5. 如果上传 `.github/workflows/AKI_star_volume.yml` 不方便，就先进仓库里的 `.github/workflows` 文件夹，再上传这个 yml 文件
6. 页面底部写提交说明，例如：`添加 AKI 星星量筛选器`
7. 点 `Commit changes`

## 4. Server 酱怎么操作

1. 打开 Server 酱官网：`https://sct.ftqq.com/`
2. 用微信扫码登录
3. 登录后进入 `SendKey` 页面
4. 找到你的 SendKey，格式通常像 `SCTxxxxxxxxxxxxxxxxxxxx`
5. 复制这个 SendKey
6. 回到 GitHub 仓库
7. 点仓库上方 `Settings`
8. 左侧点 `Secrets and variables`
9. 点 `Actions`
10. 点 `New repository secret`
11. `Name` 填：`AKI_SERVER_CHAN_SENDKEY`
12. `Secret` 粘贴刚才复制的 Server 酱 SendKey
13. 点 `Add secret`

重点：这次不要用原来的 `SERVER_CHAN_SENDKEY`，要用新的 `AKI_SERVER_CHAN_SENDKEY`，这样两个任务互不影响。

## 5. 手动运行一次 GitHub Action

1. 回到 GitHub 仓库首页
2. 点上方 `Actions`
3. 左侧找到 `AKI Star Volume Stock Scanner`
4. 点进去
5. 点右侧 `Run workflow`
6. 分支选择 `main`
7. 再点绿色 `Run workflow`

运行成功后，你微信会收到一条标题带 `AKI星星量股票筛选` 的推送。

## 6. 自动运行时间

现在设置的是：

北京时间周一到周五 18:10 左右自动运行。

你的旧筛选器如果是其他时间跑，这个 AKI 版本不会和它同名，也不会共用同一个 Secret。

## 7. 本地测试

如果你想在自己电脑上先测试：

```bash
cd /Users/jiangyuehan/Desktop/AKI星星量股票筛选器
pip install -r AKI_requirements.txt
python AKI_star_volume_scanner.py --no-push --limit 20
```

这会只扫描一小部分股票，并且不推送，只生成本地报告。

## 8. 后面怎么调筛选条件

如果推送太少，可以放宽这些参数：

- `volume_shrink_ratio` 调大一点
- `tight_price_range` 调大一点
- `near_low_ratio` 调大一点

如果推送太多，可以反过来调小。

筛选结果只是技术形态观察，不是买卖建议。

