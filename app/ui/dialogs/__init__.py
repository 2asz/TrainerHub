"""dialogs 包：公共门面（原 dialogs.py 按对话框职责拆分，对外导出不变）。

四个外部调用方（main_window / detail_panel / mw_mixins / tasks）仍从
本包导入，拆分后一行都不用改。私有 worker 是所属对话框的实现细节
（信号契约由对话框定义），跟着对话框走、不并入 tasks.py，此处一并
re-export 让旧导入路径继续可用。"""
from ._common import (_StyledDialog, game_dir_name, save_local_cover,
                      trainer_dest_dir)
from .game_dialog import _SteamSearchWorker, AddGameDialog, EditGameDialog
from .settings_dialog import SettingsDialog
from .trainer_add import _TrainerAddWorker, AddTrainerDialog
from .trainer_download import (_AutoUpdateProbeWorker, _DownloadWorker,
                               DownloadDialog)
from .trainer_scan import _ScanAcceptWorker, ScanResultDialog

__all__ = ["_StyledDialog", "_SteamSearchWorker", "_TrainerAddWorker",
           "_AutoUpdateProbeWorker", "_DownloadWorker", "_ScanAcceptWorker",
           "AddGameDialog", "AddTrainerDialog", "DownloadDialog",
           "EditGameDialog", "ScanResultDialog", "SettingsDialog",
           "game_dir_name", "save_local_cover", "trainer_dest_dir"]
