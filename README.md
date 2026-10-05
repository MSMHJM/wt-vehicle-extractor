# WT Vehicle Extractor

War Thunder 载具资源提取与 Blender 重建工具。项目提供本地 HTML 工作台，也保留命令行提取入口。

## 特性

- 从 GRP 资源包扫描飞机、坦克和军舰
- 扫描结果按工作目录缓存，并根据资源包修改时间自动失效
- 使用 `content.hq` 高清贴图
- 导出 Blender 层级、材质、玻璃和可动部件
- 读取 `_animtree` 中的 `dirAxis` 并为旋转部件创建约束
- 支持 HTML UI、后台任务、实时日志、路径诊断和缓存清理

## 使用

要求：Windows、Python 3.12+、Blender，以及可用的 DAE 解析源码或可联网自动获取 DAE。

启动 HTML 工作台：

```powershell
python wt_web.py
```

浏览器打开：

```text
http://127.0.0.1:8765/
```

首次使用时，在页面填写游戏 `content/base/res` 路径和 `blender.exe` 路径。配置将保存到工作目录的 `wt_config.json`，该文件不会写入仓库。

命令行示例：

```powershell
python wt_extract.py --name su_27 --res "D:\War Thunder\content\base\res" --blender "D:\Blender\blender.exe"
```

## 目录缓存

扫描缓存保存在工作目录的 `.wt-cache` 文件夹中，并根据资源包修改时间和大小自动失效。用户配置保存在工作目录的 `wt_config.json`。提取过程产生的临时解析文件会按选项清理。

## 许可证

本项目使用 MIT License，详见 [LICENSE](LICENSE)。

War Thunder、Dagor Engine 及其相关资源归各自权利人所有。本项目不包含游戏资源，也不代表相关权利人。
