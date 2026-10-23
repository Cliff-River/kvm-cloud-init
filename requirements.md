# 需求：增加 `start`、`stop`、`reboot`、`status` 子命令 及 等待 `cloud-init`完成

## 一、子命令
1. `start` 命令：启动实例
2. `stop` 命令：停止实例， `--force` 参数强制强制停止
3. `reboot` 命令：重启实例
4. `status` 命令：查询实例状态
5. 原来的`destroy`命令服用`stop`命令的关机逻辑，避免代码重复。

## 二、等待 `cloud-init` 完成
1. `create` 命令创建虚拟机后，默认等待 `cloud-init` 完成，用户可以通过 `--no-wait` 参数禁用等待 `cloud-init` 完成。
2. 在等待过程中，用户随时通过`Ctrl+C`退出等待。
3. 当存在`--verbose`参数时，以少于7行的空间滚动刷新`cloud-init`的输出。
4. 当`cloud-init`失败，打印错误信息。

## 三、增加`cloud-init`子命令
1. 增加`cloud-init` 子命令，查询`cloud-init`状态。
2. `kvmci cloud-init status` 查询`cloud-init`状态。
3. `kvmci cloud-init log` 查询`cloud-init`详细日志，`-f`/`--follow`参数表示实时跟随日志，直至`cloud-init`完成。

## 四、其它
1. 所有命令提示使用英文，避免使用中文。原有的一些命令提示使用了中文，需要修改为英文。
2. 修改文档说明。