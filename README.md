# libadb-ios

把 [adb-mobile](https://github.com/wsvn53/adb-mobile) 的 ADB 官方源码在 GitHub Actions 上编译成 iOS 静态库 `libadb.a`。

## 这是什么

`adb-mobile` 把 Android 官方的 `android-tools`（含 `adb` 命令行客户端）移植成可在 iOS App 内集成的静态库。
本仓库不修改其源码，只提供一套可在 macOS runner 上跑通的 CI，用于验证并产出可用的 `libadb.a`。

## 为什么用它

相比用 Swift/Kotlin 重写 ADB 协议，它有三个不可替代的优势：

| 能力 | 说明 |
|:---|:---|
| **配对** | `commandline.cpp` 里包含 `pair` 子命令，TLS 1.3 + SPAKE2 + X.509 自签证书全套来自 ADB 官方源码 |
| **行为一致** | 同一份 C++ 代码，超时、截断、`delayed_ack` 协商等细节与桌面 adb 逐字节一致 |
| **命令全量** | `connect` / `install` / `shell` / `push` / `pull` / `forward` / `logcat` / `reboot` / `sideload` / `tcpip` / `root` 全在 |

## 产物

```
dist/
├── libadb-full.a        # 静态库（iphoneos/arm64）
└── include/
    └── adb_public.h     # 对外接口
```

## 构建

推送到 `main` 会自动触发；也可在 Actions 页面手动 `Run workflow`。

## 已知限制

- **只支持 TCP/IP**：`usb_libusb.cpp` 与 `usb_osx.cpp` 在上游是空文件，无 USB 直连。
- **只编 iphoneos/arm64**：模拟器切片可改 Makefile 目标自行追加。
- 上游最后更新 2025-12，若 Xcode 升级导致编译失败需自行适配。

## 许可证

本仓库仅含 CI 配置，不分发上游源码。
`adb-mobile` 上游基于 Android `android-tools`（Apache-2.0），集成时请遵守其许可证。
