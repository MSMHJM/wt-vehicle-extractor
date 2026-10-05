# -*- coding: utf-8 -*-
"""
wt_build.py — 由 wt_extract.py 自动调用，在 Blender 内运行。

作用: 读取 geometry.json (.mtl + textures)，重建带真实枢轴的节点层级，
      再按类别分组合并，保存为 .blend。

用法(通常无需手动调用):
  blender -b --factory-startup -P wt_build.py -- <geometry.json> <mtl> <texdir> <out.blend> <name>
"""
import json
import math
import os
import sys

import bpy
from mathutils import Matrix, Vector

# ---------------------------------------------------------------- 参数
argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
if len(argv) < 5:
    raise SystemExit("用法: ... -P wt_build.py -- <geometry.json> <mtl> <texdir> <out.blend> <name> [animtree.json]")
GEOM, MTL, TEXDIR, OUT_BLEND, NAME = argv[:5]
ANIM = argv[5] if len(argv) > 5 else ""

# Dagor -> Blender 轴转换 (x,y,z) -> (x,-z,y)
AXIS = Matrix(((1, 0, 0, 0), (0, 0, -1, 0), (0, 1, 0, 0), (0, 0, 0, 1)))
IDENT = Matrix.Identity(4)

# 类别关键词（按顺序匹配，可自行增补）
CATEGORIES = [
    ("机翼",   ("spar", "wing", "aileron", "flap")),
    ("尾翼",   ("tail", "fin", "elevator", "rudder", "stab")),
    ("起落架", ("gear", "wheel")),
    ("座舱",   ("cockpit", "seat", "blister", "pilot", "canopy", "glass", "hatch")),
    ("发动机", ("engine", "nozzle", "intake", "jet_flame", "oil", "exhaust", "prop", "turbine")),
    ("武器",   ("pylon", "gun", "cannon", "missile", "bomb", "rocket", "weapon",
                "flare", "rack", "r60", "r40", "aso", "su_", "torpedo", "launcher")),
    ("机身",   ("fuse", "fuselage", "hull", "body", "frame", "armor")),
]


def categorize(nodename):
    low = nodename.lower()
    for cat, keys in CATEGORIES:
        if any(k in low for k in keys):
            return cat
    return "其它"


def mat4(flat):
    return Matrix((flat[0:4], flat[4:8], flat[8:12], flat[12:16]))


def log(m):
    print(f"[build] {m}", flush=True)


# ---------------------------------------------------------------- 载入数据
data = json.load(open(GEOM, encoding="utf-8"))
nodes = data["nodes"]
objects = data["objects"]
node_by_name = {n["name"]: n for n in nodes}
children = {}
for n in nodes:
    children.setdefault(n["parent"], []).append(n["name"])

# ---------------------------------------------------------------- 清空场景
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene


def link(ob):
    scene.collection.objects.link(ob)


# ---------------------------------------------------------------- 材质
GLASS_KEYWORD = "glass"


def set_input(bsdf, names, value):
    for n in names:
        if n in bsdf.inputs:
            bsdf.inputs[n].default_value = value
            return True
    return False


def make_glass(m):
    """把材质调成玻璃：透明 + 轻微反射。

    WT 的玻璃材质（如 dynamic_glass_chrome）没有贴图，Principled 默认底色是白的，
    渲染出来就是一块不透明的白板。这里改成透明混合 + 低粗糙度，
    反射由太阳/环境的高光提供，看起来才像真玻璃。
    """
    bsdf = m.node_tree.nodes.get("Principled BSDF")
    if bsdf is None:
        return
    if not bsdf.inputs["Base Color"].is_linked:
        set_input(bsdf, ("Base Color",), (0.78, 0.85, 0.90, 1.0))
    set_input(bsdf, ("Roughness",), 0.05)
    set_input(bsdf, ("IOR",), 1.45)
    set_input(bsdf, ("Alpha",), 0.25)
    if hasattr(m, "blend_method"):
        m.blend_method = 'BLEND'
    if hasattr(m, "surface_render_method"):
        m.surface_render_method = 'BLENDED'
    if hasattr(m, "use_screen_refraction"):
        m.use_screen_refraction = True


def load_materials():
    mats = {}
    if not MTL or not os.path.isfile(MTL):
        return mats
    cur = None
    mapping = {}
    classes = {}
    for line in open(MTL, encoding="utf-8", errors="ignore"):
        line = line.strip()
        if line.startswith("newmtl "):
            cur = line[7:].strip()
            mapping[cur] = None
        elif line.startswith("#wt_class ") and cur:
            classes[cur] = line[9:].strip()
        elif line.lower().startswith("map_kd ") and cur:
            mapping[cur] = line[7:].strip()
    for name, tex in mapping.items():
        m = bpy.data.materials.new(name)
        m.use_nodes = True
        bsdf = m.node_tree.nodes.get("Principled BSDF")
        if tex:
            path = os.path.join(TEXDIR, tex)
            img = None
            if os.path.isfile(path):
                try:
                    img = bpy.data.images.load(path)
                except Exception as e:
                    log(f"贴图载入失败 {tex}: {e}")
            if img is not None:
                tn = m.node_tree.nodes.new("ShaderNodeTexImage")
                tn.image = img
                tn.location = (-400, 0)
                m.node_tree.links.new(tn.outputs["Color"], bsdf.inputs["Base Color"])
                # 注意: WT 的 _c 贴图 Alpha 通道不是不透明度(实测多数贴图 alpha
                # 均值仅 0.03~0.7)，若接到 Principled Alpha 并开启 HASHED 混合，
                # 模型会大面积抖动镂空、看起来"贴图乱"。这里只取 RGB，保持不透明。
                if hasattr(m, "blend_method"):
                    m.blend_method = 'OPAQUE'
        # 玻璃类材质(座舱盖/舷窗等)单独处理成透明+反射
        if GLASS_KEYWORD in classes.get(name, "").lower():
            make_glass(m)
        mats[name] = m
    return mats


MATS = load_materials()
log(f"材质 {len(MATS)} 个，贴图目录 {TEXDIR}")


def material_for(mname):
    if not mname:
        return None
    mat = MATS.get(mname) or bpy.data.materials.get(mname)
    if mat is None:
        mat = bpy.data.materials.new(mname)
        MATS[mname] = mat
    return mat


# ---------------------------------------------------------------- 先建网格数据
# Blender 5.2 不允许把 Mesh 赋给 EMPTY 物体的 .data，因此网格在
# 创建物体时一并传入。先为每个几何体建好 Mesh。
mesh_by_name = {}
for g in objects:
    gname = g["name"]
    verts = g["verts"]

    # 蒙皮网格 / 没有对应骨架节点的网格：Dagor 里这些顶点不做节点变换，
    # 已经是模型空间坐标。先乘节点 wtm 的逆转回节点局部坐标，
    # 之后再由节点自身的变换放回正确位置，避免被重复变换。
    if g.get("worldspace"):
        n = node_by_name.get(gname)
        if n is not None:
            inv = mat4(n["wtm"]).inverted()
            verts = [tuple(inv @ Vector(v)) for v in verts]

    me = bpy.data.meshes.new(gname)
    me.from_pydata([tuple(v) for v in verts], [], [tuple(f) for f in g["faces"]])
    me.update()

    uvl = me.uv_layers.new(name="UVMap")
    uvsrc = g["uvs"]
    for poly in me.polygons:
        for li in poly.loop_indices:
            vi = me.loops[li].vertex_index
            uvl.data[li].uv = uvsrc[vi]

    slots = {}
    for mname in dict.fromkeys(g["fmat"]):
        mat = material_for(mname)
        if mat is None:
            continue
        slots[mname] = len(me.materials)
        me.materials.append(mat)
    for poly in me.polygons:
        mname = g["fmat"][poly.index]
        if mname in slots:
            poly.material_index = slots[mname]

    mesh_by_name[gname] = me

log(f"网格数据 {len(mesh_by_name)} 个")

# ---------------------------------------------------------------- 根 / 层级
# 骨架根节点(通常是空名节点)折叠进顶层物体，其 tm 并入 AXIS
roots = children.get(None, [])
if len(roots) == 1:
    top_tm = AXIS @ mat4(node_by_name[roots[0]]["tm"])
    part_names = children.get(roots[0], [])
else:
    top_tm = AXIS
    part_names = list(roots)

top = bpy.data.objects.new(NAME, None)
top.empty_display_size = 0.5
link(top)
top.matrix_basis = top_tm

nodeobj = {}
axis_removed = 0
groups_kept = 0


def make_node(node_name, parent_obj, prefix=None):
    """建节点物体。

    骨架里没有同名网格的节点分两种：
      * 有子件 —— 是 WT 的分组枢轴（spar_l_dm / tail_dm / gear_l_dm 等），
        必须保留成空物体，否则机翼、尾翼没法整组旋转，小零件也不会跟随；
      * 没子件 —— 真正的废轴（b13_001 / emtr_break_* 等），折叠掉：
        把它的变换乘进子节点，不留物体。
    """
    global axis_removed, groups_kept
    n = node_by_name[node_name]
    acc = mat4(n["tm"]) if prefix is None else prefix @ mat4(n["tm"])
    me = mesh_by_name.pop(node_name, None)
    kids = children.get(node_name, [])
    if me is None:
        if not kids:
            axis_removed += 1
            return
        ob = bpy.data.objects.new(node_name, None)
        link(ob)
        ob.parent = parent_obj
        ob.matrix_parent_inverse = IDENT
        ob.matrix_basis = acc
        nodeobj[node_name] = ob
        groups_kept += 1
        for c in kids:
            make_node(c, ob)
        return
    ob = bpy.data.objects.new(node_name, me)
    link(ob)
    ob.parent = parent_obj
    ob.matrix_parent_inverse = IDENT
    ob.matrix_basis = acc
    nodeobj[node_name] = ob
    for c in kids:
        make_node(c, ob)


for p in part_names:
    make_node(p, top)

# 剩下没有对应骨架节点的几何体直接挂到顶层
nmesh = 0
for gname, me in mesh_by_name.items():
    ob = bpy.data.objects.new(gname, me)
    link(ob)
    ob.parent = top
    ob.matrix_parent_inverse = IDENT
    ob.matrix_basis = IDENT
    nodeobj[gname] = ob
    nmesh += 1

log(f"节点物体 {len(nodeobj)} 个 / 无节点网格 {nmesh} 个 / "
    f"分组枢轴 {groups_kept} 个 / 折叠废轴 {axis_removed} 个")

# ---------------------------------------------------------------- 按类别合并
cat_empty = {}
cat_members = {}
# 归类对象 = 折叠纯轴后直接挂在 top 下的那些部件
for ob in list(top.children):
    cat = categorize(ob.name)
    if cat not in cat_empty:
        ce = bpy.data.objects.new(cat, None)
        link(ce)
        ce.parent = top
        ce.matrix_parent_inverse = IDENT
        ce.matrix_basis = IDENT
        cat_empty[cat] = ce
    cat_members[cat] = cat_members.get(cat, 0) + 1
    # 类别空物体与 top 的世界矩阵完全相同，所以只改父级、保留 matrix_basis 即可
    # 维持部件的世界位置与枢轴。切勿在这里读写 matrix_world：它要等依赖图更新
    # 后才是最新值，此时读到的还是旧值，写回去会把所有部件的变换清零导致错位。
    ob.parent = cat_empty[cat]
    ob.matrix_parent_inverse = IDENT

bpy.context.view_layer.update()
log("类别: " + ", ".join(f"{k}({cat_members[k]})" for k in cat_empty))

# ---------------------------------------------------------------- 可动部件：转轴 + 限角
# 转轴优先用 _animtree 里读出来的 dirAxis（Dagor 把 rotateNode 的轴声明成 vec3f，
# 是节点局部空间的真实铰链轴）。读不到时回退：部件在自身局部空间里包围盒最长的
# 那一维就是铰链方向（舵面都是"细长"的，长度方向即铰链线）。
# 限角仍按部件类型的保守默认值：源文件里的 kMul/kMul2 是两段线性系数（实测
# 出现过 0.5 这种非角度值），不是限角，所以不拿来当角度用。
MOVABLE_LIMITS = (
    ("flaperon", 25.0),
    ("aileron", 25.0),
    ("flap", 40.0),
    ("slat", 25.0),
    ("elevator", 20.0),
    ("rudder", 30.0),
)
AXES = ("x", "y", "z")


def movable_rule(name):
    """部件名 -> (关键词, 默认限角)。"""
    low = name.lower()
    for key, lim in MOVABLE_LIMITS:
        if key in low:
            return key, lim
    return None, None


def hinge_axis(ob):
    """回退方案：在物体局部空间里用包围盒最长的一维当铰链轴。"""
    lo = [1e9] * 3
    hi = [-1e9] * 3
    for v in ob.data.vertices:
        for i in range(3):
            lo[i] = min(lo[i], v.co[i])
            hi[i] = max(hi[i], v.co[i])
    ext = [hi[i] - lo[i] for i in range(3)]
    return max(range(3), key=lambda i: ext[i]), ext


def dagor_axis_to_local(vec):
    """_animtree 的 dirAxis -> Blender 物体的局部轴 (下标, 符号)。

    节点物体的 matrix_basis 直接就是 Dagor 骨架的 tm（整体 (x,-z,y) 换算只做在
    顶层 top 上），所以节点局部轴与 Dagor 轴一一对应，这里不能再做一次换算。
    实测（Su-27）：局部 Y 对升降舵=世界展向、对方向舵=世界竖直，正是各自真实铰链。
    轴不是轴对齐的则返回 None，交给几何回退。
    """
    idx = max(range(3), key=lambda i: abs(vec[i]))
    dom = vec[idx]
    if abs(abs(dom) - 1.0) > 0.02:
        return None
    if any(abs(vec[i]) > 0.05 for i in range(3) if i != idx):
        return None
    return idx, (1 if dom > 0 else -1)


# 关键词 -> (轴下标, 符号, 原始 dirAxis, kMul)；同一关键词取出现最多的那个轴
ANM_KEYS = ("flaperon", "aileron", "flap", "slat", "elevator", "rudder",
            "airbrake", "nozzle", "radiator", "wheel", "prop", "hook",
            "hatch", "door", "fan", "gear")


def anm_keyword(*texts):
    for t in texts:
        low = (t or "").lower()
        for k in ANM_KEYS:
            if k in low:
                return k
    return None


anim_axis = {}
if ANIM and os.path.isfile(ANIM):
    anm = json.load(open(ANIM, encoding="utf-8"))
    tally = {}
    for c in anm.get("controls", []):
        key = anm_keyword(c.get("param")) or anm_keyword(*c.get("nodes", []))
        conv = dagor_axis_to_local(c["axis"])
        if key is None or conv is None:
            continue
        slot = tally.setdefault(key, {})
        hit = slot.get(conv)
        if hit is None:
            slot[conv] = [1, c]
        else:
            hit[0] += 1
    for key, slot in tally.items():
        conv, (n, c) = max(slot.items(), key=lambda kv: kv[1][0])
        anim_axis[key] = (conv[0], conv[1], c["axis"], c.get("kMul"), n)
    if anim_axis:
        log("源文件转轴: " + ", ".join(
            f"{k}={AXES[v[0]].upper()}{'+' if v[1] > 0 else '-'}x{v[4]}"
            for k, v in sorted(anim_axis.items())))
    else:
        log("动画树里没有可用的轴对齐转轴，全部回退到几何判定")
else:
    log("未提供 animtree.json，可动部件回退到几何判定转轴")

cand = {}
for ob in bpy.data.objects:
    if ob.type != 'MESH':
        continue
    key, lim = movable_rule(ob.name)
    if key:
        cand[ob.name] = (key, lim)
# 几何回退的包围盒判定要用世界矩阵做"轴是否竖直"的检查，先刷新依赖图
bpy.context.view_layer.update()

# 机翼舵面（副翼/襟翼/前缘缝翼）不可能绕竖直轴铰接。若源文件给出的轴方向在
# 世界里几乎竖直，说明这条 animtree 记录对应的骨架节点不是这个网格（通常是
# 分组枢轴，帧朝向与铰链无关），这时改用几何判定。
WING_KEYS = ("aileron", "flaperon", "flap", "slat")
fitted = []
vetoed = []
for nm in sorted(cand):
    ob = bpy.data.objects[nm]
    key, lim = cand[nm]
    src = anim_axis.get(key)
    if src is not None and key in WING_KEYS:
        probe = Vector((0.0, 0.0, 0.0))
        probe[src[0]] = 1.0
        if abs((ob.matrix_world.to_3x3() @ probe).normalized().z) > 0.9:
            vetoed.append(f"{nm}(源轴竖直)")
            src = None
    if src is not None:
        idx, sign, raw_axis, k_mul, _n = src
        how = "animtree"
    else:
        # 顶点太少的小碎片（如只有 4 个顶点的 slat1_l）包围盒不可靠，跳过
        if len(ob.data.vertices) < 20:
            continue
        idx, _ext = hinge_axis(ob)
        sign, raw_axis, k_mul = 1, None, None
        how = "几何"
    # 子段（如 flaperon1_l 挂在 flaperon_l 下）不重复加约束
    anc, nested = ob.parent, False
    while anc is not None:
        if anc.name in cand:
            nested = True
            break
        anc = anc.parent
    if nested:
        continue
    ax = AXES[idx]
    ob.rotation_mode = 'XYZ'
    rest = ob.rotation_euler[idx]          # 限角要相对静止姿态，否则部件会被直接拽到极限
    c = ob.constraints.new('LIMIT_ROTATION')
    c.owner_space = 'LOCAL'
    for i, a in enumerate(AXES):
        setattr(c, f"use_limit_{a}", i == idx)
    setattr(c, f"min_{ax}", rest - math.radians(lim))
    setattr(c, f"max_{ax}", rest + math.radians(lim))
    # 保留源文件里的转轴与方向，供后续做非对称限角/动画时使用
    if raw_axis is not None:
        ob["wt_axis_dagor"] = [float(x) for x in raw_axis]
        ob["wt_axis_sign"] = sign
    if k_mul is not None:
        ob["wt_kMul"] = float(k_mul)
    ob["wt_limit_deg"] = lim
    fitted.append(f"{nm}[{ax.upper()}{'+' if sign > 0 else '-'}±{lim:g}°/{how}]")
log(f"可动部件限角 {len(fitted)} 个: " + ", ".join(fitted))
if vetoed:
    log("源轴不合理已改用几何判定: " + ", ".join(vetoed))

# ---------------------------------------------------------------- 打包贴图并保存
for img in list(bpy.data.images):
    if img.source == 'FILE' and not img.packed_file:
        try:
            img.pack()
        except Exception:
            pass

os.makedirs(os.path.dirname(OUT_BLEND), exist_ok=True)
bpy.ops.wm.save_as_mainfile(filepath=OUT_BLEND)
log(f"已保存 {OUT_BLEND}")
log("BUILD DONE")
