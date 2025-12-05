from gsuid_core.logger import get_all_log_path


def get_history_logs_page():
    all_log_path = get_all_log_path()
    ACTION = {
        "actionType": "dialog",
        "dialog": {
            "body": {
                "id": "u:448f2ad2a090",
                "type": "form",
                "title": "查看数据",
                "mode": "flex",
                "labelAlign": "top",
                "dsType": "api",
                "feat": "View",
                "body": [
                    {
                        "name": "id",
                        "label": "id",
                        "row": 0,
                        "type": "input-number",
                    },
                    {
                        "name": "时间",
                        "label": "时间",
                        "row": 1,
                        "type": "input-text",
                    },
                    {
                        "name": "日志等级",
                        "label": "日志等级",
                        "row": 2,
                        "type": "select",
                    },
                    {
                        "name": "模块",
                        "label": "模块",
                        "row": 3,
                        "type": "input-text",
                    },
                    {
                        "name": "内容",
                        "label": "内容",
                        "row": 4,
                        "type": "textarea",
                    },
                ],
                "static": True,
                "actions": [
                    {
                        "type": "button",
                        "actionType": "cancel",
                        "label": "关闭",
                    }
                ],
                "onEvent": {
                    "submitSucc": {
                        "actions": [
                            {
                                "actionType": "search",
                                "groupType": "component",
                                "componentId": "u:b1f51d7a324a",
                            }
                        ]
                    }
                },
            },
            "title": "查看数据",
            "size": "md",
            "actions": [
                {
                    "type": "button",
                    "actionType": "cancel",
                    "label": "关闭",
                }
            ],
        },
    }
    data = {
        "type": "page",
        "title": "历史日志过滤查询",
        "id": "u:908f0ee5fa73",
        "asideResizor": False,
        "pullRefresh": {"disabled": True},
        "body": [
            {
                "type": "select",
                "label": "选择日期",
                "name": "select",
                "options": [
                    {"label": i.name, "value": i.name} for i in all_log_path
                ],
                "id": "u:30962bfb9f83",
                "multiple": False,
                "onEvent": {
                    "change": {
                        "weight": 0,
                        "actions": [
                            {
                                "componentId": "u:b1f51d7a324a",
                                "ignoreError": False,
                                "actionType": "reload",
                                "data": {"date": "${event.data.value}"},
                                "dataMergeMode": "merge",
                            }
                        ],
                    }
                },
            },
            {
                "type": "divider",
                "id": "u:d1c9f48ea328",
                "lineStyle": "solid",
                "direction": "horizontal",
                "rotate": 0,
            },
            {
                "id": "u:b1f51d7a324a",
                "type": "crud2",
                "mode": "table2",
                "dsType": "api",
                "syncLocation": True,
                "primaryField": "id",
                "loadType": "pagination",
                "api": {
                    "url": "/genshinuid/api/historyLogs",
                    "method": "get",
                    "requestAdaptor": "",
                    "adaptor": "",
                    "messages": {},
                    "dataType": "json",
                },
                "filter": {
                    "type": "form",
                    "title": "条件查询",
                    "mode": "inline",
                    "columnCount": 3,
                    "clearValueOnHidden": True,
                    "behavior": ["SimpleQuery"],
                    "body": [
                        {
                            "name": "id",
                            "label": "id",
                            "type": "input-number",
                            "size": "full",
                            "required": False,
                            "behavior": "SimpleQuery",
                            "id": "u:7d5dfd6fb4a5",
                            "keyboard": True,
                            "step": 1,
                        },
                        {
                            "name": "时间",
                            "label": "时间",
                            "type": "input-text",
                            "size": "full",
                            "required": False,
                            "behavior": "SimpleQuery",
                            "id": "u:946cd5942b15",
                        },
                        {
                            "name": "日志等级",
                            "label": "日志等级",
                            "type": "select",
                            "size": "full",
                            "required": False,
                            "behavior": "SimpleQuery",
                            "id": "u:d0bf26304939",
                            "multiple": False,
                            "options": [
                                {"label": "INFO", "value": "INFO"},
                                {"label": "SUCCESS", "value": "SUCCESS"},
                                {"label": "WARNING", "value": "WARNING"},
                                {"label": "ERROR", "value": "ERROR"},
                                {"label": "DEBUG", "value": "DEBUG"},
                                {"label": "TRACE", "value": "TRACE"},
                            ],
                            "selectFirst": False,
                            "removable": False,
                            "clearable": True,
                            "checkAll": False,
                            "joinValues": True,
                        },
                        {
                            "name": "模块",
                            "label": "模块",
                            "type": "input-text",
                            "size": "full",
                            "required": False,
                            "behavior": "SimpleQuery",
                            "id": "u:294ae6f80ff2",
                        },
                        {
                            "name": "内容",
                            "label": "内容",
                            "type": "textarea",
                            "size": "full",
                            "required": False,
                            "behavior": "SimpleQuery",
                            "id": "u:ee5fc2b996d7",
                        },
                    ],
                    "actions": [
                        {
                            "type": "reset",
                            "label": "重置",
                            "id": "u:22b8ade74682",
                        },
                        {
                            "type": "submit",
                            "label": "查询",
                            "level": "primary",
                            "id": "u:e853a21b2077",
                        },
                    ],
                    "id": "u:2a882fb870a0",
                    "feat": "Insert",
                },
                "headerToolbar": [
                    {
                        "type": "flex",
                        "direction": "row",
                        "justify": "flex-start",
                        "alignItems": "stretch",
                        "style": {"position": "static"},
                        "items": [
                            {
                                "type": "container",
                                "align": "left",
                                "behavior": [
                                    "Insert",
                                    "BulkEdit",
                                    "BulkDelete",
                                ],
                                "body": [],
                                "wrapperBody": False,
                                "style": {
                                    "flexGrow": 1,
                                    "flex": "1 1 auto",
                                    "position": "static",
                                    "display": "flex",
                                    "flexBasis": "auto",
                                    "flexDirection": "row",
                                    "flexWrap": "nowrap",
                                    "alignItems": "stretch",
                                    "justifyContent": "flex-start",
                                },
                                "id": "u:f7dee2ce6250",
                            },
                            {
                                "type": "container",
                                "align": "right",
                                "behavior": ["FuzzyQuery"],
                                "body": [],
                                "wrapperBody": False,
                                "style": {
                                    "flexGrow": 1,
                                    "flex": "1 1 auto",
                                    "position": "static",
                                    "display": "flex",
                                    "flexBasis": "auto",
                                    "flexDirection": "row",
                                    "flexWrap": "nowrap",
                                    "alignItems": "stretch",
                                    "justifyContent": "flex-end",
                                },
                                "id": "u:8d2dc85fed4b",
                            },
                        ],
                        "id": "u:14f26e218177",
                    }
                ],
                "footerToolbar": [
                    {
                        "type": "flex",
                        "direction": "row",
                        "justify": "flex-start",
                        "alignItems": "stretch",
                        "style": {"position": "static"},
                        "items": [
                            {
                                "type": "container",
                                "align": "left",
                                "body": [],
                                "wrapperBody": False,
                                "style": {
                                    "flexGrow": 1,
                                    "flex": "1 1 auto",
                                    "position": "static",
                                    "display": "flex",
                                    "flexBasis": "auto",
                                    "flexDirection": "row",
                                    "flexWrap": "nowrap",
                                    "alignItems": "stretch",
                                    "justifyContent": "flex-start",
                                },
                                "id": "u:ffc293c3af21",
                            },
                            {
                                "type": "container",
                                "align": "right",
                                "body": [
                                    {
                                        "type": "pagination",
                                        "behavior": "Pagination",
                                        "layout": [
                                            "total",
                                            "perPage",
                                            "pager",
                                            "go",
                                        ],
                                        "perPage": 10,
                                        "perPageAvailable": [10, 20, 50, 100],
                                        "align": "right",
                                        "id": "u:515f5d3a63d3",
                                        "size": "",
                                    }
                                ],
                                "wrapperBody": False,
                                "style": {
                                    "flexGrow": 1,
                                    "flex": "1 1 auto",
                                    "position": "static",
                                    "display": "flex",
                                    "flexBasis": "auto",
                                    "flexDirection": "row",
                                    "flexWrap": "nowrap",
                                    "alignItems": "stretch",
                                    "justifyContent": "flex-end",
                                },
                                "id": "u:6b708bcf8cf1",
                            },
                        ],
                        "id": "u:6cedb9797d50",
                    }
                ],
                "columns": [
                    {
                        "type": "tpl",
                        "title": "id",
                        "name": "id",
                        "id": "u:f360a70eb838",
                    },
                    {
                        "type": "tpl",
                        "title": "时间",
                        "name": "时间",
                        "id": "u:40ba8ecccd71",
                    },
                    {
                        "type": "mapping",
                        "title": "日志等级",
                        "name": "日志等级",
                        "id": "u:8af643bd0487",
                        "placeholder": "-",
                        "map": {
                            "*": "<span class='label label-default'>其他</span>",
                            "ERROR": "<span class='label label-danger'>错误</span>",  # noqa: E501
                            "WARNING": "<span class='label label-label label-warning'>警告</span>",  # noqa: E501
                            "SUCCESS": "<span class='label label-success'>成功</span>",  # noqa: E501
                            "DEBUG": '<span class="label" style="background-color: rgb(58, 118, 251); color: white;">调试</span>',  # noqa: E501
                            "INFO": '<span class="label" style="background-color: rgb(140, 140, 140); color: rgb(255, 255, 255);">正常</span>',  # noqa: E501
                            "TRACE": '<span class="label" style="background-color: rgb(235, 62, 247); color: white;">追溯</span>',  # noqa: E501
                        },
                    },
                    {
                        "type": "tpl",
                        "title": "模块",
                        "name": "模块",
                        "id": "u:561fed6e37bf",
                    },
                    {
                        "type": "tpl",
                        "title": "内容",
                        "name": "内容",
                        "id": "u:306a49203d90",
                    },
                    {
                        "type": "operation",
                        "title": "操作",
                        "buttons": [
                            {
                                "type": "button",
                                "label": "查看",
                                "level": "link",
                                "behavior": "View",
                                "onEvent": {"click": {"actions": [ACTION]}},
                                "id": "u:2a40b63939db",
                            }
                        ],
                        "id": "u:b1aead938964",
                    },
                ],
                "editorSetting": {
                    "mock": {"enable": True, "maxDisplayRows": 5}
                },
                "loadDataOnce": True,
                "showHeader": True,
            },
        ],
    }
    return data
