import hashlib
from pathlib import Path

# 本脚本位于 tools/ 下，项目根目录是其父目录
root = Path(__file__).resolve().parent.parent
pkg = root / 'dist_包' / '中大深圳充电桩看板-一键启动包'


def h(p):
    return hashlib.md5(Path(p).read_bytes()).hexdigest()[:10]


print('=== 同步后一致性校验 ===')
for f in ['index.html', 'app.js', 'styles.css', 'gz-swiss.css']:
    a = root / 'web' / f
    b = pkg / 'web' / f
    print(('OK  ' if h(a) == h(b) else 'DIFF'), f)

a = root / 'README.md'
b = pkg / '说明文档' / 'README.md'
print(('OK  ' if h(a) == h(b) else 'DIFF'), '说明文档/README.md')

s = (pkg / 'web' / 'app.js').read_text(encoding='utf-8')
print('包内 app.js 含 renderFinder:', 'renderFinder' in s)
print('包内 app.js 含 FLAGS.full:', 'FLAGS.full' in s)
h2 = (pkg / 'web' / 'index.html').read_text(encoding='utf-8')
print('包内 index.html 含 finder section:', 'id="finder"' in h2)
