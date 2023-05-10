from pathlib import Path

# raw_url = 'https://raw.githubusercontent.com'
# repo_title = 'Genshin-bots/GenshinUID-docs/master/docs'
# plugins_lib = f'{raw_url}/{repo_title}/PluginsList.md'
plugins_lib = 'https://docs.gsuid.gbots.work/plugin_list.json'

proxy_url = 'https://ghproxy.com/'

PLUGINS_PATH = Path(__file__).parents[2] / 'plugins'
CORE_PATH = Path(__file__).parents[3]
