# OpenAlice terminal interaction port

本目录的键盘、IME、Kitty 模式、renderer 与输入辅助模块移植自 OpenAlice
revision `c02a3c7d7d438783897e3f7610714b121c41367c`，并按 OpenCLI 的认证
WebSocket、Fleet 多路复用和会话状态协议进行适配。上游代码适用父目录保留的
`LICENSE-OpenAlice.txt`（AGPL-3.0）；这不表示其适用仓库的 Apache 许可证。

控制平面只签发短期用户票据并转发终端帧。PTY、回放、控制权和清理证明均由
已保留的隔离节点维护；浏览器断开不会停止进程。
