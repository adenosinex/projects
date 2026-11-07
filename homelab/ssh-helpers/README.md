# ssh-helpers

通过 Tailscale 隧道连 Mac 的几个小工具（密码只走环境变量 `SSHPASS`，从不写进文件）：

- `tailscale-ssh-proxy.py` — 给 OpenSSH ProxyCommand 用的 stdin/socket 转发器（隧道代理端口 3130）
- `ssh-paramiko.py` — 用法：`SSHPASS=... python ssh-paramiko.py <host> <user> '<命令>'`
- `ssh-sftp.py` — 用法：`SSHPASS=... python ssh-sftp.py put|get <本地> <远程>`
