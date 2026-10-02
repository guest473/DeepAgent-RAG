import os
from utils.path_tool import get_abs_path

def load_prompts(prompt_name):
    abs_path = get_abs_path(os.path.join('prompts_data', prompt_name))
    try:
        with open(abs_path, 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        print(f"提示词文件不存在: {abs_path}")
    except Exception as e:
        print(f"加载提示词失败 ({abs_path}): {e}")

def load_main_prompt():
    return load_prompts('main_prompt.txt')
def load_summary_prompt():
    return load_prompts('summary_prompt.txt')
