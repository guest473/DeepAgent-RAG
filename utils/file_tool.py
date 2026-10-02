import os
import stat
import hashlib
import threading
from os import listdir
from langchain_community.document_loaders import TextLoader, PyPDFLoader, Docx2txtLoader
from utils.path_tool import get_abs_path

# md5.txt 的全局读写锁，index_health 等模块复用同一把锁，防止并发读写损坏文件
md5_lock = threading.RLock()

# 日志约定：本模块只输出异常路径与一次性事件；例行的逐文件状态（MD5 值、md5.txt 增删、
# 存在性判断）一律不打印——这些调用在构建/自检时按文件数放大，会把日志刷掉

# 文件 MD5 缓存：路径 -> (大小, mtime_ns, md5)
# 列表接口、健康自检等会反复对同一批文件求 MD5，按 stat 命中可避免每次重读整个文件；
# 文件被改动后大小或 mtime 变化即自动失效重算。
_md5_cache: dict[str, tuple[int, int, str]] = {}
_md5_cache_lock = threading.Lock()
_MD5_CACHE_MAX_ENTRIES = 20000


def get_file_md5_hex(file_path):
    # file_path 来自 allowed_type_files_list，已是绝对路径
    try:
        st = os.stat(file_path)
    except OSError:
        print(f"文件{file_path}不存在")
        return None
    if not stat.S_ISREG(st.st_mode):
        print(f"{file_path}不是文件")
        return None

    cache_key = os.path.normcase(file_path)
    with _md5_cache_lock:
        cached = _md5_cache.get(cache_key)
        if cached and cached[0] == st.st_size and cached[1] == st.st_mtime_ns:
            return cached[2]

    md5obj = hashlib.md5()
    try:
        with open(file_path, 'rb') as f:
            while chunk := f.read(65536):
                md5obj.update(chunk)
        md5_hex = md5obj.hexdigest()
    except OSError as e:
        print(f"获取文件{file_path}的md5值时出错: {e}")
        return None

    with _md5_cache_lock:
        if len(_md5_cache) >= _MD5_CACHE_MAX_ENTRIES:
            _md5_cache.clear()
        _md5_cache[cache_key] = (st.st_size, st.st_mtime_ns, md5_hex)
    return md5_hex

def add_file_md5_hex_to_file(md5_hex):
    md5txt_file_path = get_abs_path(os.path.join("md5.txt"))
    with md5_lock:
        with open(md5txt_file_path, "a", encoding='utf-8') as f:
            f.write(f"{md5_hex}\n")


def check_file_md5_hex_in_file(md5_hex):
    md5txt_file_path = get_abs_path(os.path.join("md5.txt"))
    with md5_lock:
        if not os.path.exists(md5txt_file_path):
            return False
        with open(md5txt_file_path, "r", encoding='utf-8') as f:
            for line in f:
                if line.strip() == md5_hex:
                    return True
            return False


def read_md5_set() -> set:
    """一次性读取 md5.txt 为集合。

    需要批量判断多个文件是否已入库时使用，避免逐条重新扫描整个记录文件。
    """
    md5txt_file_path = get_abs_path(os.path.join("md5.txt"))
    with md5_lock:
        if not os.path.exists(md5txt_file_path):
            return set()
        with open(md5txt_file_path, "r", encoding='utf-8') as f:
            return {line.strip() for line in f if line.strip()}

def remove_file_md5_hex_from_file(md5_hex):
    md5txt_file_path = get_abs_path(os.path.join("md5.txt"))
    with md5_lock:
        if not os.path.exists(md5txt_file_path):
            print(f"md5.txt不存在，无法删除{md5_hex}")
            return
        with open(md5txt_file_path, "r", encoding='utf-8') as f:
            lines = f.readlines()
        new_lines = [line for line in lines if line.strip() != md5_hex]
        # 原子替换：先写临时文件再 rename，避免中途失败截断损坏 md5.txt
        tmp_path = md5txt_file_path + ".tmp"
        with open(tmp_path, "w", encoding='utf-8') as f:
            f.writelines(new_lines)
        os.replace(tmp_path, md5txt_file_path)

def list_files_with_allowed_type(dir_path,allowed_types:tuple[str]):
    abs_dir_path = get_abs_path(dir_path)
    files = []
    if not os.path.exists(abs_dir_path):
        os.makedirs(abs_dir_path)
        print(f"目录{abs_dir_path}不存在，现已新创建")
        return files
    if not os.path.isdir(abs_dir_path):
        print(f"{abs_dir_path}不是目录")
        return files
    for f in listdir(abs_dir_path):
        ext = os.path.splitext(f)[1].lower()
        if ext in allowed_types:
            files.append(get_abs_path(os.path.join(dir_path, f)))
    return tuple(files) #防止篡改

def txt_loader(file_path):
    return TextLoader(file_path, encoding='utf-8').load()

def pdf_loader(file_path):
    return PyPDFLoader(file_path).load()

def docx_loader(file_path):
    return Docx2txtLoader(file_path).load()

def choose_loader(file_path):#这里的file_path是从allowed_type_files_list获取的，已经是绝对路径
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".txt":
        return txt_loader
    elif ext == ".pdf":
        return pdf_loader
    elif ext == ".docx":
        return docx_loader
    else:
        print(f"不支持的文件类型{file_path}")
        return
