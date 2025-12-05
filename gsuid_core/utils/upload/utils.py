from gsuid_core.utils.plugins_config.gs_config import pic_upload_config

is_auto_delete: bool = pic_upload_config.get_config("AutoDelete").data
