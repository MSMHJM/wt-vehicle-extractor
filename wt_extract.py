#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
War Thunder 载具模型 一键提取脚本（系统 Python 3.12 运行）

用法:
  python wt_extract.py --name mig_25pd
  python wt_extract.py --name cn_mbt2000 --lod 0
  python wt_extract.py --name mig_25pd --no-build          # 只提取，不跑 Blender

流程:
  1. 准备依赖(DAE，缺失时从镜像克隆；pylzma 垫片)
  2. 在 res\{aircrafts,tanks,ships} 里定位载具所在的 .grp
  3. 用 DAE 解析 DynModel + 骨架(GeomNodeTree) + 材质/贴图
  4. 导出中间数据: geometry.json(节点局部网格) / skeleton.json / *.mtl / textures
  5. 调用 Blender 运行 wt_build.py，重建带枢轴的层级并按类别合并，输出 .blend
  6. 清理临时文件与 DAE 克隆
"""
import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wt_config.json")
DAE_MIRRORS = (
    "https://ghfast.top/https://github.com/quentin-dh/Dagor-Asset-Explorer.git",
    "https://github.com/quentin-dh/Dagor-Asset-Explorer.git",
)
VEHICLE_FOLDERS = ("aircrafts", "tanks", "ships")
AGGREGATE_PACKS = ("air.grp", "tanks.grp", "ships.grp")

# 特效广告牌(尾焰/螺旋桨模糊等)材质关键词；这些不是实体部件，默认剔除。
# 若某载具需要保留，加 --keep-effects。
EFFECT_MATS = ("jet_flame", "propmask", "billboard", "muzzle_flash", "tracer")


def _is_effect(material_name):
    low = (material_name or "").lower()
    return any(k in low for k in EFFECT_MATS)

PYLZMA_SHIM = '''# minimal pylzma shim (py3.12 has no wheel) - raw LZMA1
import lzma
def _filters(h):
    props = h[0]; ds = int.from_bytes(h[1:5], "little")
    lc = props % 9; rem = props // 9; lp = rem % 5; pb = rem // 5
    return [{"id": lzma.FILTER_LZMA1, "lc": lc, "lp": lp, "pb": pb, "dict_size": max(ds, 4096)}]
def decompress(data):
    return lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=_filters(data)).decompress(data[5:])
def compress(data, preset=9):
    c = lzma.LZMACompressor(format=lzma.FORMAT_RAW, filters=[{"id": lzma.FILTER_LZMA1, "preset": preset}])
    return bytes([93]) + (1 << 24).to_bytes(4, "little") + c.compress(data) + c.flush()
'''

HERE = os.path.dirname(os.path.abspath(__file__))


def log(msg):
    print(f"[wt] {msg}", flush=True)


def rmtree(path):
    """删除目录，遇到只读文件(git objects)时先改权限再删。"""
    def onerr(func, p, exc):
        try:
            os.chmod(p, 0o700)
            func(p)
        except Exception:
            pass
    shutil.rmtree(path, ignore_errors=True, onerror=onerr)


# --------------------------------------------------------------------------
# 依赖准备
# --------------------------------------------------------------------------
def ensure_shim(workdir):
    shim_dir = os.path.join(workdir, "shim")
    os.makedirs(shim_dir, exist_ok=True)
    path = os.path.join(shim_dir, "pylzma.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(PYLZMA_SHIM)
    return shim_dir


def ensure_dae(workdir, dae_dir=None):
    """返回包含 parse/ util/ 的 DAE 源码目录。缺失时从镜像克隆。"""
    if dae_dir and os.path.isdir(os.path.join(dae_dir.strip().strip("`"), "parse")):
        return dae_dir.strip().strip("`")
    if dae_dir:
        clean_dae = dae_dir.strip().strip("`")
        if os.path.isdir(os.path.join(clean_dae, "src", "dae", "parse")):
            return os.path.join(clean_dae, "src", "dae")

    clone_root = os.path.join(workdir, "DAE")
    root = os.path.join(clone_root, "src", "dae")
    if os.path.isdir(os.path.join(root, "parse")):
        return root

    if shutil.which("git") is None:
        raise RuntimeError("未找到 git，无法自动获取 DAE，请用 --dae 指定已克隆的 DAE 目录")

    last_err = None
    for index, raw_url in enumerate(DAE_MIRRORS):
        url = str(raw_url).replace("`", "").strip()
        clone = tempfile.mkdtemp(prefix=f"dae_clone_{index}_", dir=workdir)
        target = os.path.join(clone, "DAE")
        root = os.path.join(target, "src", "dae")
        log(f"克隆 DAE: {url}")
        try:
            subprocess.run(["git", "clone", "--depth", "1", url, target], check=True)
        except subprocess.CalledProcessError as e:
            last_err = e
            rmtree(clone)
            continue
        if os.path.isdir(os.path.join(root, "parse")):
            return root
        last_err = RuntimeError(f"克隆完成但目录结构无效: {target}")
        rmtree(clone)
    raise RuntimeError(f"DAE 克隆失败: {last_err}")


# --------------------------------------------------------------------------
# 定位载具所在资源包
# --------------------------------------------------------------------------
def _pack_has_name(pack_path, name):
    from parse.gameres import GameResourcePack
    grp = GameResourcePack(pack_path)
    for i in range(grp.getRealResEntryCnt()):
        if grp.getRealResEntry(i).getName() == name:
            return True
    return False


def _name_cache_path(res_root, category):
    digest = hashlib.sha1(os.path.abspath(res_root).encode("utf-8")).hexdigest()[:12]
    cache_dir = os.path.join(HERE, ".wt-cache")
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, f"wt_names_{digest}_{category}.json")


def _grp_signature(paths):
    return [(p, os.path.getmtime(p), os.path.getsize(p)) for p in paths]


def list_vehicle_names(res_root, category, dae_root, refresh=False):
    from parse.gameres import GameResourcePack

    folders = {"飞机": "aircrafts", "坦克": "tanks", "军舰": "ships"}
    selected = folders.get(category)
    folders_to_scan = [selected] if selected else list(folders.values())
    pack_paths = []
    for current in folders_to_scan:
        directory = os.path.join(res_root, current)
        if os.path.isdir(directory):
            pack_paths.extend(os.path.join(directory, f) for f in os.listdir(directory) if f.endswith(".grp"))
    pack_paths.sort()
    cache_path = _name_cache_path(res_root, category)
    signature = _grp_signature(pack_paths)
    if not refresh and os.path.isfile(cache_path):
        try:
            cached = json.load(open(cache_path, encoding="utf-8"))
            if cached.get("signature") == signature:
                return cached.get("names", [])
        except (OSError, ValueError, TypeError):
            pass

    candidates = {}
    for pack_path in pack_paths:
        try:
            grp = GameResourcePack(pack_path)
            entries = {grp.getRealResEntry(i).getName() for i in range(grp.getRealResEntryCnt())}
        except Exception:
            continue
        for entry in entries:
            if entry.endswith(("_skeleton", "_anim", "_animtree", "_dmg", "_xray", "_skinned")):
                continue
            if entry + "_skeleton" in entries:
                candidates.setdefault(entry, pack_path)
    names = sorted(candidates)
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump({"signature": signature, "names": names}, f, ensure_ascii=False)
    except OSError:
        pass
    return names


def _legacy_list_vehicle_names(res_root, category, dae_root):
    from parse.gameres import GameResourcePack

    folders = {"飞机": "aircrafts", "坦克": "tanks", "军舰": "ships"}
    folder = folders.get(category)
    if folder is None:
        folders = {"飞机": "aircrafts", "坦克": "tanks", "军舰": "ships"}
        folders_to_scan = list(folders.values())
    else:
        folders_to_scan = [folder]
    candidates = {}
    for current in folders_to_scan:
        directory = os.path.join(res_root, current)
        if not os.path.isdir(directory):
            continue
        for filename in os.listdir(directory):
            if not filename.endswith(".grp"):
                continue
            pack_path = os.path.join(directory, filename)
            try:
                grp = GameResourcePack(pack_path)
                entries = {grp.getRealResEntry(i).getName() for i in range(grp.getRealResEntryCnt())}
            except Exception:
                continue
            for entry in entries:
                if entry.endswith("_skeleton") or entry.endswith("_anim") or entry.endswith("_animtree"):
                    continue
                if entry.endswith(("_dmg", "_xray", "_skinned")):
                    continue
                if entry + "_skeleton" in entries:
                    candidates.setdefault(entry, pack_path)
    return sorted(candidates)


def find_pack(res_root, name):
    # 1) 每个载具分类目录下的同名 grp
    for folder in VEHICLE_FOLDERS:
        p = os.path.join(res_root, folder, name + ".grp")
        if os.path.isfile(p):
            return p
    # 2) 聚合包（air.grp / tanks.grp / ships.grp）
    for folder in VEHICLE_FOLDERS:
        for ag in AGGREGATE_PACKS:
            p = os.path.join(res_root, folder, ag)
            if os.path.isfile(p) and _pack_has_name(p, name):
                return p
    for ag in AGGREGATE_PACKS:
        p = os.path.join(res_root, ag)
        if os.path.isfile(p) and _pack_has_name(p, name):
            return p
    # 3) 兜底：遍历分类目录下所有 grp
    for folder in VEHICLE_FOLDERS:
        d = os.path.join(res_root, folder)
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if not fn.endswith(".grp"):
                continue
            p = os.path.join(d, fn)
            if _pack_has_name(p, name):
                return p
    raise RuntimeError(f"在 {res_root} 下未找到载具 '{name}' 的资源包")


# --------------------------------------------------------------------------
# 材质描述 / 贴图包
# --------------------------------------------------------------------------
def load_desc(res_root):
    """载入 riDesc.bin / dynModelDesc.bin —— 载具材质与贴图名都从这里解析。"""
    from parse.gameres import GameResDesc
    from util.assetcacher import AssetCacher

    loaded = []
    for fn in ("dynModelDesc.bin", "riDesc.bin"):
        p = os.path.join(res_root, fn)
        if os.path.isfile(p):
            grd = GameResDesc(p)
            AssetCacher.appendGameResDesc(grd)
            grd.loadDataBlock()
            loaded.append(fn)
    if not loaded:
        log("警告: 未找到 *Desc.bin，材质/贴图将不可用")
    return loaded


def hq_texture_roots(res_root):
    """WT 的高清贴图不在 content/base/res 里，而在同级的 content.hq 下。

    content.hq 里是按载具命名的独立贴图包（如 ger_vs10_hydrofoil.dxp.bin），
    同一张贴图的分辨率远高于基础包：vs10_hydrofoil_c 基础包只有 512x256，
    高清包是 4096x2048。所以提取时必须把这些目录也扫上。
    """
    roots = []
    p = os.path.abspath(res_root)
    for _ in range(4):
        p = os.path.dirname(p)
        if not p or os.path.dirname(p) == p:
            break
        cand = os.path.join(p, "content.hq")
        if os.path.isdir(cand):
            roots.append(cand)
            break
    return roots


def cache_textures(res_root, mdl):
    """扫描贴图包，把模型用到的 DDSx 贴图注册进缓存。

    WT 把同一张贴图按画质分级存放在不同的包里（zzz-tq_pack 为最低画质缩略版，
    <国别>_gm_bq 为基础画质，*-hq 为高清）。若"先找到算数"，常常会抓到最低画质
    的那份，贴图会明显发糊。因此这里遍历全部资源包，同一张贴图取分辨率最高者。
    """
    from util.assetcacher import AssetCacher
    from parse.material import DDSxTexturePack2

    needed = set()
    for mat in (mdl.materials or ()):
        for t in (getattr(mat, "textures", None) or ()):
            needed.add(t.split("*")[0])
    needed.discard("")
    if not needed:
        return 0

    best = {}   # 贴图名 -> (评分, ddsx)；评分 = (像素数, 是否高清包)
    roots = [res_root] + hq_texture_roots(res_root)
    for scan_root in roots:
        for root, _dirs, files in os.walk(scan_root):
            for fn in files:
                if not fn.endswith(".dxp.bin"):
                    continue
                is_hq = "hq" in fn.lower()
                try:
                    pack = DDSxTexturePack2(os.path.join(root, fn))
                    entries = pack.getPackedFiles()
                except Exception:
                    continue
                for ddsx in entries:
                    if ddsx is None:
                        continue
                    nm = ddsx.name.split("*")[0]
                    if nm not in needed:
                        continue
                    try:
                        pixels = ddsx.getPixelCnt()
                    except Exception:
                        pixels = 0
                    score = (pixels, 1 if is_hq else 0)
                    if nm not in best or score > best[nm][0]:
                        best[nm] = (score, ddsx)

    for _score, ddsx in best.values():
        AssetCacher.cacheAsset(ddsx)

    missing = needed - set(best)
    if missing:
        log(f"警告: 未找到贴图 {sorted(missing)}")
    if best:
        # 贴图质量只取决于资源包，与所选 LOD 无关：任何 LOD 都用最高画质的那份。
        lowest = min(v[0][0] for v in best.values())
        log(f"贴图取自最高画质包(含 content.hq 高清包，与 LOD 无关): "
            f"{len(best)} 张，最小 {lowest} 像素")
    return len(best)


def _camo_base_slot(material):
    """舰船材质(dynamic_masked_ship)的槽1是迷彩底色(camo_*)，槽0(_c)是贴花图集。

    WT 里船体底色来自迷彩，图集只是叠加的贴花/钢板细节，而且图集用的是另一套
    UV(网格里存的 UV0 是给迷彩/平铺贴图用的)。若把图集当底色，船体就会出现
    错乱的斜条纹。所以这里挑出该用哪个槽作为 map_Kd。
    """
    if "ship" not in (getattr(material, "cls", "") or ""):
        return None
    try:
        slots = material.getTextureSlots()
    except Exception:
        return None
    if len(slots) < 2:
        return None
    mask = slots[1]
    if mask and mask.split("*")[0].startswith("camo_"):
        return mask
    return None


def patch_ship_camo(mdl, ydir):
    """把舰船材质的 map_Kd 从贴花图集换成迷彩底色，并导出对应的迷彩贴图。"""
    camo_of = {}
    for mat in (mdl.materials or ()):
        camo = _camo_base_slot(mat)
        if camo is None:
            continue
        try:
            mat.exportTexture(camo, ydir)
        except Exception as e:
            log(f"迷彩贴图导出失败 {camo}: {e}")
            continue
        camo_of[mat.getName()] = "textures/" + camo.split("*")[0] + ".dds"
    if not camo_of:
        return 0

    mtl = os.path.join(ydir, f"{mdl.exportName}.mtl")
    if not os.path.isfile(mtl):
        return 0
    out, cur, changed = [], None, 0
    for line in open(mtl, encoding="utf-8", errors="ignore"):
        s = line.strip()
        if s.startswith("newmtl "):
            cur = s[7:].strip()
        elif s.lower().startswith("map_kd ") and cur in camo_of:
            out.append(f"\tmap_Kd {camo_of[cur]}\n")
            changed += 1
            continue
        out.append(line)
    with open(mtl, "w", encoding="utf-8") as f:
        f.writelines(out)
    return changed


def annotate_material_classes(mdl, ydir):
    """在 .mtl 里为每个材质写入 `#wt_class <着色器类>` 注释。

    Blender 侧据此区分材质类型：玻璃类(dynamic_glass_chrome 等)要做成透明+反射，
    而普通车体/机体贴图必须保持不透明。
    """
    mtl = os.path.join(ydir, f"{mdl.exportName}.mtl")
    if not os.path.isfile(mtl):
        return 0
    cls_of = {m.getName(): (getattr(m, "cls", "") or "") for m in (mdl.materials or ())}
    out, cur, n = [], None, 0
    for line in open(mtl, encoding="utf-8", errors="ignore"):
        if line.strip().startswith("newmtl "):
            cur = line.strip()[7:].strip()
            out.append(line)
            cls = cls_of.get(cur)
            if cls:
                out.append(f"#wt_class {cls}\n")
                n += 1
            continue
        out.append(line)
    with open(mtl, "w", encoding="utf-8") as f:
        f.writelines(out)
    return n


# --------------------------------------------------------------------------
# 动画树(_animtree)：可动部件的真实转轴与方向
# --------------------------------------------------------------------------
# WT 的 _animtree(.anm) 是 Dagor 的二进制 DataBlock。实测布局：
#   0x00            magic "\0ac2"
#   0x18 + 8        参数记录流起点
#   0x28            本块数据区基址(恒为 0x30)
#   0x30 + 8        向量数据池起点，POINT3 参数的值就是相对此处的字节偏移
#   0x3c            字段名个数；字段名从 0x48 起、NUL 分隔
# 记录 = 8 字节 [value:u32][nameId:u16][type:u16]，nameId 是字段名下标，
# type 用 Dagor 的 ParamType(TYPE_STRING=1 / TYPE_REAL=3 / TYPE_POINT3=5 /
# TYPE_BOOL=9)。其中 dirAxis 声明为 vec3f，即节点局部空间里的旋转轴。
ANM_STRING, ANM_REAL, ANM_POINT3, ANM_BOOL = 1, 3, 5, 9
ANM_VEC_BASE_EXTRA = 8      # 向量池 = u32@0x28 + 8
ANM_REC_BASE_EXTRA = 8      # 记录流 = u32@0x18 + 8
ANM_FIELDS_OFF = 0x48

# 可动部件关键词(与 wt_build 的 MOVABLE_LIMITS 对应)，用于把 dirAxis 归类
ANM_CONTROL_KEYS = ("flaperon", "aileron", "flap", "slat", "elevator", "rudder",
                    "airbrake", "nozzle", "radiator", "wheel", "prop", "hook",
                    "hatch", "door", "fan", "gear")


def _anm_string(data, off):
    """还原 .anm 里的字符串。

    字符串池是 NUL 分隔的，但记录里的偏移会指向某个串的中间(如落在
    'changeRate' 的 'angeRate' 处)。真实串 = 该偏移所在的整个 NUL 分隔
    token，所以要向前回退到上一个 NUL。落进浮点池的“串”会带不可打印字符，
    直接判为无效。
    """
    if not 0 < off < len(data):
        return None
    end = data.find(b"\0", off)
    if end < 0 or end - off > 256:
        return None
    text = data[data.rfind(b"\0", 0, off) + 1:end]
    if not text or not all(32 <= c < 127 for c in text):
        return None
    return text.decode("ascii")


def parse_animtree(data):
    """解析 _animtree，返回每个 rotateNode 的 {param, nodes, axis, kMul, ...}。"""
    if len(data) < ANM_FIELDS_OFF or data[:4] != b"\x00ac2":
        return []
    field_cnt = struct.unpack_from("<I", data, 0x3c)[0]
    if not 0 < field_cnt <= 4096:
        return []
    fields, p = [], ANM_FIELDS_OFF
    while len(fields) < field_cnt:
        e = data.find(b"\0", p)
        if e < 0:
            break
        fields.append(data[p:e].decode("latin1"))
        p = e + 1
    if "dirAxis" not in fields:
        return []

    vec_base = struct.unpack_from("<I", data, 0x28)[0] + ANM_VEC_BASE_EXTRA
    rec0 = struct.unpack_from("<I", data, 0x18)[0] + ANM_REC_BASE_EXTRA
    if not 0 < rec0 < len(data) or vec_base >= len(data):
        return []
    cnt = (len(data) - rec0) // 8
    recs = []
    for i in range(cnt):
        v, fid, typ = struct.unpack_from("<IHH", data, rec0 + 8 * i)
        recs.append((v, fields[fid] if fid < len(fields) else "", typ))

    out = []
    for i, (v, fname, typ) in enumerate(recs):
        if fname != "dirAxis" or typ != ANM_POINT3:
            continue
        off = vec_base + v
        if not 0 < off <= len(data) - 12:
            continue
        axis = list(struct.unpack_from("<3f", data, off))
        if not all(-1.001 <= c <= 1.001 for c in axis):
            continue        # 偏移没落在向量池里，丢弃
        info = {"axis": [round(c, 5) for c in axis], "param": None, "nodes": [],
                "kMul": None, "kMul2": None, "relativeRot": False}
        # 本节点所在记录的尾部：param/paramAdd/relativeRot 紧跟 dirAxis，
        # 目标节点名则排在 param 之前；再往前就是上一个节点的 kMul/kMul2，
        # 遇到它们说明已越过边界。
        j = i - 1
        while j >= 0 and i - j <= 24:
            v2, f2, t2 = recs[j]
            if f2 in ("kMul", "kMul2", "dirAxis"):
                break
            if f2 == "param" and t2 == ANM_STRING and info["param"] is None:
                info["param"] = _anm_string(data, v2)
            elif f2 in ("targetNode", "node", "nodeRE") and t2 == ANM_STRING:
                s = _anm_string(data, v2)
                if s:
                    info["nodes"].append(s.lstrip("~^"))
            j -= 1
        for j in range(i + 1, min(cnt, i + 5)):
            v2, f2, t2 = recs[j]
            if f2 == "kMul" and t2 == ANM_REAL and info["kMul"] is None:
                info["kMul"] = round(struct.unpack_from("<f", data, rec0 + 8 * j)[0], 5)
            elif f2 == "kMul2" and t2 == ANM_REAL and info["kMul2"] is None:
                info["kMul2"] = round(struct.unpack_from("<f", data, rec0 + 8 * j)[0], 5)
            elif f2 == "relativeRot" and t2 == ANM_BOOL:
                info["relativeRot"] = bool(v2)
        info["nodes"] = sorted(set(info["nodes"]))
        out.append(info)
    return out


def export_animtree(name, entries, outdir):
    """导出 _animtree 的解析结果到 animtree.json，供 wt_build 使用。"""
    entry = entries.get(name + "_animtree")
    if entry is None:
        log("未找到 _animtree，可动部件将回退到几何判定转轴")
        return ""
    try:
        controls = parse_animtree(entry.getRealResData().getBin().read())
    except Exception as e:
        log(f"动画树解析失败(回退到几何判定转轴): {e}")
        return ""
    if not controls:
        log("动画树里没有 dirAxis 记录，回退到几何判定转轴")
        return ""
    path = os.path.join(outdir, "animtree.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"name": name, "controls": controls}, f, ensure_ascii=False)
    axes = {}
    for c in controls:
        key = tuple(round(x, 3) for x in c["axis"])
        axes[key] = axes.get(key, 0) + 1
    summary = ", ".join(f"({k[0]:g},{k[1]:g},{k[2]:g})x{v}" for k, v in axes.items())
    log(f"动画树: {len(controls)} 个可动节点，转轴 {summary}")
    return path


# --------------------------------------------------------------------------
# 提取
# --------------------------------------------------------------------------
def pick_main_model(entries, name):
    """资源名与 grp 文件名常不一致(如 grp 为 cn_mbt2000，资源却是 mbt2000)。
    在包内挑出主 DynModel：有配套 _skeleton、且排除 _dmg/_xray 等变体。"""
    cands = [n for n in entries if (n + "_skeleton") in entries
             and not n.endswith(("_dmg", "_xray", "_skinned"))]
    if not cands:
        cands = [n for n in entries if (n + "_skeleton") in entries]
    if not cands:
        return None
    for c in cands:
        if c in name or name in c:
            return c
    return min(cands, key=len)


def extract(pack_path, name, lod, outdir, res_root, keep_effects=False):
    from parse.gameres import GameResourcePack
    from parse.realres import GeomNodeTree, DynModel
    from util.assetcacher import AssetCacher

    load_desc(res_root)

    grp = GameResourcePack(pack_path)
    entries = {}
    for i in range(grp.getRealResEntryCnt()):
        e = grp.getRealResEntry(i)
        entries[e.getName()] = e

    if name not in entries:
        alt = pick_main_model(entries, name)
        if alt is None:
            raise RuntimeError(f"资源包 {pack_path} 中不含 '{name}'，可用名: {sorted(entries)}")
        log(f"'{name}' 未命中，自动改用资源名 '{alt}'")
        name = alt

    dyn_entry = entries[name]
    skel_entry = entries.get(name + "_skeleton")

    # 骨架必须先注册进缓存，DynModel 才能解析
    if skel_entry is not None:
        skel = skel_entry.getRealResData()
        AssetCacher.cacheAsset(skel)
        skel.__retrieveData__()
    else:
        log(f"警告: 未找到 {name}_skeleton")
        skel = None

    dyn = dyn_entry.getRealResData()
    if not isinstance(dyn, DynModel):
        raise RuntimeError(f"'{name}' 不是 DynModel (classId={hex(dyn.classId)})")

    # LOD 0 是最高细节模型，编号越大细节越低。越界时回退到 LOD 0。
    dyn.computeData()   # lodCount 只有解析后才可读
    lod_cnt = dyn.lodCount
    if lod < 0 or lod >= lod_cnt:
        log(f"LOD {lod} 不存在：'{name}' 共 {lod_cnt} 个 LOD (0~{lod_cnt - 1})，已回退到最高细节 LOD 0")
        lod = 0
    else:
        log(f"'{name}' 共 {lod_cnt} 个 LOD，本次使用 LOD {lod}（LOD 0 为最高细节）")

    mdl = dyn.getModel(lod)

    if mdl.materials is None:
        log("警告: 未解析到材质（检查 res 下是否有 *Desc.bin）")

    # 材质 + 贴图（借助 DAE 自带的 OBJ 导出写 .mtl 和贴图文件，然后丢掉 .obj）
    ydir = os.path.join(outdir, "y")
    os.makedirs(ydir, exist_ok=True)
    ntex = cache_textures(res_root, mdl) if mdl.materials is not None else 0
    log(f"材质 {len(mdl.materials or ())} 个，贴图 {ntex} 张")
    obj_path = None
    if mdl.materials is not None:
        mdl.exportObj(ydir, exportTexture=True)
        obj_path = os.path.join(ydir, f"{mdl.exportName}.obj")
        annotate_material_classes(mdl, ydir)
        n_camo = patch_ship_camo(mdl, ydir)
        if n_camo:
            log(f"舰船迷彩底色: {n_camo} 个材质改用迷彩贴图作为 map_Kd（贴花图集不适合当底色）")

    # 动画树：可动部件的真实转轴/方向（读不到就回退到几何判定）
    anm_path = export_animtree(name, entries, outdir)

    # 骨架节点
    nodes = []
    if skel is not None:
        for n in skel.getNodes():
            nodes.append({
                "name": n.name,
                "parent": n.parent.name if n.parent else None,
                "tm": [v for row in n.tm for v in row],
                "wtm": [v for row in n.wtm for v in row],
            })

    # 逐物体网格（局部坐标；worldspace 标记的物体顶点为模型空间坐标）
    log(f"导出网格几何: {sum(1 for _ in mdl)} 个物体")
    skel_names = {n["name"] for n in nodes}
    objects = []
    skipped = 0
    for o in mdl:
        omats = {o.materials[k] for k in o.materials}
        if not keep_effects and omats and all(_is_effect(m) for m in omats):
            skipped += 1
            continue
        mat_keys = sorted(o.materials.keys())

        def mat_for(fi):
            mn = None
            for k in mat_keys:
                if k <= fi:
                    mn = o.materials[k]
                else:
                    break
            return mn

        vmap, verts, uvs, faces, fmat = {}, [], [], [], []
        for fi, face in enumerate(o.faces):
            tri = []
            for vid in face:
                idx = vmap.get(vid)
                if idx is None:
                    idx = len(verts)
                    vmap[vid] = idx
                    verts.append([round(c, 5) for c in mdl.getVertex(vid)])
                    uvs.append([round(c, 6) for c in mdl.getUV(vid)])
                tri.append(idx)
            faces.append(tri)
            fmat.append(mat_for(fi))
        # Dagor 只对"非蒙皮且有同名骨架节点"的网格做节点变换；其余(蒙皮网格、
        # 没有对应节点的网格)顶点已是模型空间坐标。Blender 重建时需区别对待。
        objects.append({"name": o.name, "skinned": o.skinned,
                        "worldspace": bool(o.skinned) or (o.name not in skel_names),
                        "verts": verts, "uvs": uvs, "faces": faces, "fmat": fmat})

    if skipped:
        log(f"已剔除 {skipped} 个特效广告牌（--keep-effects 可保留）")

    data = {"name": name, "exportName": mdl.exportName, "lod": lod, "lodCount": lod_cnt,
            "nodes": nodes, "objects": objects}
    geom_path = os.path.join(outdir, "geometry.json")
    with open(geom_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    log(f"已写出 {geom_path} ({os.path.getsize(geom_path)//1024} KB)")

    if obj_path and os.path.isfile(obj_path):
        os.remove(obj_path)
        log("已删除中间 .obj（仅保留 .mtl 与贴图）")

    return {
        "geometry": geom_path,
        "mtl": os.path.join(ydir, f"{mdl.exportName}.mtl") if mdl.materials is not None else "",
        "textures": ydir,
        "animtree": anm_path,
    }


# --------------------------------------------------------------------------
def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            value = json.load(f)
            return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def main():
    config = load_config()
    ap = argparse.ArgumentParser(description="War Thunder 载具模型一键提取")
    ap.add_argument("--name", default=None, help="载具资源名，如 mig_25pd / cn_mbt2000")
    ap.add_argument("--category", default="其它载具", choices=("飞机", "坦克", "军舰", "其它载具"), help="扫描载具类别")
    ap.add_argument("--list-names", action="store_true", help="扫描并输出可用载具资源名")
    ap.add_argument("--res", default=config.get("res", ""), help="War Thunder res 根目录")
    ap.add_argument("--lod", type=int, default=0, help="LOD 级别，默认 0")
    ap.add_argument("--out", default=None, help="输出目录，默认 <工作目录>/<name>_model")
    ap.add_argument("--dae", default=os.environ.get("WT_DAE"), help="已克隆的 DAE 源码目录(可选)")
    ap.add_argument("--blender", default=config.get("blender", ""), help="blender.exe 路径")
    ap.add_argument("--no-build", action="store_true", help="只提取，不运行 Blender 重建")
    ap.add_argument("--keep-effects", action="store_true", help="保留尾焰/广告牌等特效面片")
    ap.add_argument("--keep-temp", action="store_true", help="保留临时文件与 DAE 克隆")
    ap.add_argument("--refresh-scan", action="store_true", help="忽略载具列表缓存并重新扫描")
    args = ap.parse_args()

    if not args.list_names and not args.name:
        ap.error("必须提供 --name，或使用 --list-names 扫描可用载具")

    outdir = os.path.abspath(args.out or os.path.join(HERE, f"{args.name}_model")) if args.name else None
    if outdir:
        os.makedirs(outdir, exist_ok=True)

    workdir = os.path.join(tempfile.gettempdir(), "wt_extract")
    os.makedirs(workdir, exist_ok=True)
    shim_dir = ensure_shim(workdir)
    dae_root = ensure_dae(workdir, args.dae)

    sys.path.insert(0, shim_dir)
    sys.path.insert(0, dae_root)

    # DAE 依赖 PyQt5（系统 Python 已安装），仅用到对话框，正常解析不会触发
    import PyQt5  # noqa: F401

    if args.list_names:
        names = list_vehicle_names(args.res, args.category, dae_root, refresh=args.refresh_scan)
        for item in names:
            print(f"__WT_NAME__{item}", flush=True)
        log(f"扫描完成，共找到 {len(names)} 个可提取载具")
        if not args.name:
            if not args.keep_temp:
                rmtree(workdir)
            return

    log(f"定位资源包 (name={args.name}) ...")
    pack = find_pack(args.res, args.name)
    log(f"命中资源包: {pack}")

    info = extract(pack, args.name, args.lod, outdir, args.res, args.keep_effects)

    built = False
    if not args.no_build:
        build = os.path.join(HERE, "wt_build.py")
        log("调用 Blender 重建并合并 ...")
        cmd = [args.blender, "-b", "--factory-startup", "-P", build, "--",
               info["geometry"], info["mtl"], info["textures"],
               os.path.join(outdir, f"{args.name}.blend"), args.name, info["animtree"]]
        subprocess.run(cmd, check=True)
        built = True
        log(f"完成: {os.path.join(outdir, args.name + '.blend')}")

    # 清理
    if not args.keep_temp:
        rmtree(workdir)
        if built:
            # 贴图已打包进 .blend，中间产物(geometry.json / animtree.json / y 目录)可删
            for p in (info["geometry"], info["textures"], info["animtree"]):
                if p and os.path.isdir(p):
                    rmtree(p)
                elif p and os.path.isfile(p):
                    os.remove(p)
            log("已清理中间文件")
        log("已清理临时工作目录")
    else:
        log(f"临时文件保留在: {workdir}")


if __name__ == "__main__":
    main()
