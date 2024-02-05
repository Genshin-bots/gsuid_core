from typing import Dict

from .models import GSC, GsStrConfig, GsBoolConfig

PIC_UPLOAD_CONIFG: Dict[str, GSC] = {
    'PicUpload': GsBoolConfig('自动上传图片', '发送图片时将会自动上传', False),
    'PicUploadServer': GsStrConfig(
        '上传图片方式',
        '可选s3或smms或custom',
        'smms',
        ['smms', 's3', 'custom'],
    ),
    'AutoDelete': GsBoolConfig('上传完后自动删除', '是否自动删除图片', True),
    'smms_token': GsStrConfig('sm.ms_token', 'sm.ms的token', ''),
    's3_endpoint': GsStrConfig('s3_endpoint', '终结点url', ''),
    's3_access_key': GsStrConfig('s3_access_key', 'AK', ''),
    's3_secret_key': GsStrConfig('s3_secret_key', 'SK', ''),
    's3_bucket': GsStrConfig('s3_bucket', 'Bucket', ''),
    's3_region': GsStrConfig('s3_region', 'Region', ''),
    'custom_url': GsStrConfig('自定义上传图片API', '填入上传图片API', ''),
    'custom_header': GsStrConfig(
        '自定义上传图片Header', '填入上传图片hader', ''
    ),
}
