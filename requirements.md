
使用Python语言重构整个项目并实现更复杂的功能，UV作为包管理器并已初始化，要求如下：

1. 实现对KVM虚拟机对多OS多实例的管理，如快速创建和销毁等。
2. Python包和库根据标准放在 src/kvm_cloud_init/ 下。
3. templates.conf 定义模板，可通过 path 字段直接指定 cloud-init 配置，也可以直接通过 network, user-data 等字段直接配置。
4. instances.conf 定义实例（YAML格式），其可以引用某个模板创建实例，允许其在创建前覆盖模板的配置。
5. 当启动创建实例时，利用 Python 的 tmpfile 库创建一个临时文件夹，存放生成 cloud-init 配置以及后续生成的 cidata.iso。
6. default.conf 存放一些默认值。
7. 避免单个py文件代码过于繁杂，应适当解耦成多个Python包或库。 
8. 适当用 uv 安装一些第三方库，如YAML文件的解析库，清不要优先自己造轮子。
8. 编写对应的单元测试。
9. 更新文档。
10. 生成Trae 规则文件和Agent.md。