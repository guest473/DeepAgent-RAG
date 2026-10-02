import os
def get_project_root():
    current_file=os.path.abspath(__file__)
    current_dir=os.path.dirname(current_file)
    project_root=os.path.dirname(current_dir)
    return project_root
def get_abs_path(relative_path):
    return os.path.join(get_project_root(),relative_path)
