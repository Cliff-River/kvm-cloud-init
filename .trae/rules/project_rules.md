# kvm-cloud-init 项目规则

## 项目定位

多模板 / 多实例 KVM 虚拟机管理工具。Python 3.13，src 布局，uv 管理依赖，
console script 入口：`kvm-cloud-init`。

## 架构边界（不要越层）

- `cli.py`：只做 argparse 参数解析、表格输出与异常到退出码的转换，不写业务逻辑。
- `config.py`：三份 YAML 配置（default/templates/instances）的加载、校验、
  三级合并（实例 > 模板 > 默认值）。
- `cloudinit.py`：cloud-init 三份文档的渲染（实例内联 > 模板内联 > path 目录文件）。
- `seediso.py`：临时目录 + pycdlib 生成卷标 `cidata` 的 ISO。
- `qcow2.py`：qcow2 头解析、容量单位、backing chain（纯标准库）。
- `hv/`：所有 libvirt 交互。`client`（连接/存储池）、`firmware`（UEFI 检测）、
  `storage`（卷上传/扩容/删除）、`domains`（关机/快照/undefine）、
  `guest`（domain XML 生成与启动）。
- `provision.py`：编排以上模块完成 create/destroy/list/templates。
- `errors.py`：所有面向用户的异常，message 必须是可直接展示的中文。

## 硬性约定

1. 所有实例操作只连接 `qemu:///system`，禁止 `qemu:///session`。
2. 优先使用 Python 库（libvirt-python、pycdlib、PyYAML、标准库），
   不新增 subprocess 调用；`hv/storage.py` 的 `sudo -n rm` 是已论证的最后兜底。
3. 不解析 CLI 输出：libvirt XML 用 `xml.etree`，容量信息用 qcow2 头，
   禁止用正则抠 XML/JSON。
4. cloud-init 文档覆盖以整份文件为粒度，不做 YAML 深合并。
5. 全部代码加类型注解；新增配置字段必须同步更新校验逻辑、配置示例与 README。
6. 存储资源按实例命名：`<实例名>.qcow2`、`<实例名>-cidata.iso`。

## 常用命令

```bash
uv sync                  # 安装依赖
uv run pytest            # 单元测试（无需 root/KVM）
uv run kvm-cloud-init templates
uv run kvm-cloud-init list
uv run kvm-cloud-init create <实例名> [--template 模板]
uv run kvm-cloud-init destroy <实例名> [--force]
```

## 测试要求

- 新增业务逻辑必须配 pytest 用例，测试放在 `tests/`。
- 禁止在单测中建立真实 libvirt 连接或启动虚机；libvirt 对象用 fake/mock，
  XML 用字符串 fixture。
- 涉及删除文件/存储卷、undefine、关机的改动，必须有对应测试覆盖。

## 注意事项

- 三个 `.conf` 文件名虽为 .conf，但内容是 YAML。
- libvirt-python 的部分绑定不接受 flags 参数（如 `pool.info()`、`vol.info()`），
  返回的 info 是 list 而非具名元组，改动时先核对绑定签名。
- 静默 libvirt 默认错误日志的注册在 `hv/client.py`，业务需自行 try/except。
