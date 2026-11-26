# 小米路由器固件同步

从小米接口获取固件信息，按型号和版本下载。GitHub Actions 每月自动同步，并按型号发布到 Releases；也可手动触发并指定型号代号。

## 本地使用

需要 Python 3.8+，无需安装第三方依赖。

```bash
python3 miwifi_firmware.py                     # 下载已知型号
python3 miwifi_firmware.py -t RC01STA,RD03STA  # 指定型号
python3 miwifi_firmware.py --json-only         # 只保存接口数据
python3 miwifi_firmware.py --discover          # 发现新型号并下载
python3 miwifi_firmware.py --discover-only     # 更新型号列表
```

完整参数见 `python3 miwifi_firmware.py --help`。

固件保存在 `firmware/型号代号/版本/`，接口数据保存在对应型号目录。重复运行会跳过已下载的文件，支持断点续传。

## Releases

每个型号代号对应一个 Release，包含该型号的所有版本；已有同名资产会跳过。型号代号通常由设备型号和渠道后缀组成，例如 `RC01STA`（稳定版）、`R3GDEV`（开发版）。
