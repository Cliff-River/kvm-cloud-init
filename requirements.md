# 需求：实现 shell 命令连接虚拟机

通过 ssh 实现 `kvmci shell [vm-name]` 连接虚拟机，具体如下：

1. 使用第三方库生成 ssh 密钥，存储在 `keys/` 目录下。通过 `user-data` 的初始化把公钥注入到虚拟机实例中。
2. 查询虚拟机的实际IP地址，然后利用前面生成的公钥连接虚拟机实例。注意，不要相信 `network-config` 中的 IP 地址，因为有可能虚拟机没拿到这个 IP 地址。
3. 实现 `kvmci shell [vm-name]` 命令，连接虚拟机实例。
4. 实现 `kvmci exec [vm-name] -- [command]` 命令，执行虚拟机实例中的命令。