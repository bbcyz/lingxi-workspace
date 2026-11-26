# v2.1.0 · Android 与 Windows

- 修复 Android 周三充值日的设备认证回调，统一 Java 与 Python 之间的 JSON 请求和响应格式。会员权益共用的回调也已修复。
- macOS 源码运行支持钥匙串。密钥由当前用户的系统钥匙串保管，登录态、活动凭据和任务记录加密保存到统一数据目录，重启后可恢复。

1545 项 Python 测试和 278 项前端测试通过。Windows 单文件版和 ZIP 版均使用独立数据目录启动验证。Android 已核对版本及签名，并通过包内离线认证回调测试。macOS 钥匙串通过模拟验证。

## 下载

- `LingxiWorkspace-2.1.0-Android-arm64.apk` 适用于 Android 7.0+ 的 ARM64 设备。同签名覆盖安装可保留本机账号和任务。
- `LingxiWorkspace-2.1.0-Windows-x64.exe` 是 Windows x64 单文件客户端。
- `LingxiWorkspace-2.1.0-Windows-x64.zip` 是 Windows x64 文件夹版，完整解压后运行。
- `SHA256SUMS.txt` 包含三个安装包的 SHA-256 校验值。
