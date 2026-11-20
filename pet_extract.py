# -*- coding: utf-8 -*-
"""
桌宠 exe 解包助手（由生成器调用，用外部 Python 运行）。
用法: python pet_extract.py <桌宠.exe> <输出目录>
功能: 从 PyInstaller onefile 桌宠里提取 config.json、assets 帧、paint_mask，
      并把 exe 同目录 plugins/*.zip 解压成文件夹。
结果: 在 stdout 打印一行 JSON manifest（路径均为绝对路径）。
"""
import os
import sys
import json
import shutil
import zipfile
import traceback


def main():
    if len(sys.argv) < 3:
        print(json.dumps({'ok': False, 'error': '参数不足'}, ensure_ascii=False))
        return
    exe = os.path.abspath(sys.argv[1])
    out = os.path.abspath(sys.argv[2])
    os.makedirs(out, exist_ok=True)

    try:
        from PyInstaller.archive.readers import CArchiveReader
        r = CArchiveReader(exe)
        names = list(r.toc.keys())

        # ---- config.json ----
        if 'config.json' not in names:
            raise RuntimeError('这个 exe 里没有 config.json，可能不是本生成器打包的桌宠')
        cfg = json.loads(r.extract('config.json').decode('utf-8'))
        with open(os.path.join(out, 'config.json'), 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

        # ---- 素材帧 ----
        assets = os.path.join(out, 'assets')
        os.makedirs(assets, exist_ok=True)
        frame_keys = [k for k in names if os.path.basename(k.replace('\\', '/')).startswith('frame_')]
        frame_keys.sort(key=lambda k: os.path.basename(k.replace('\\', '/')))
        for k in frame_keys:
            base = os.path.basename(k.replace('\\', '/'))
            with open(os.path.join(assets, base), 'wb') as f:
                f.write(r.extract(k))

        # ---- 涂色掩膜 ----
        mask_path = ''
        for k in names:
            if 'paint_mask' in os.path.basename(k.replace('\\', '/')).lower():
                mask_path = os.path.join(out, 'paint_mask.png')
                with open(mask_path, 'wb') as f:
                    f.write(r.extract(k))
                break

        # ---- 插件 zip（exe 同目录）----
        plugins_dir = os.path.join(out, 'plugins')
        plug_infos = []
        src_plugins = os.path.join(os.path.dirname(exe), 'plugins')
        if os.path.isdir(src_plugins):
            os.makedirs(plugins_dir, exist_ok=True)
            for fn in sorted(os.listdir(src_plugins)):
                if not fn.lower().endswith('.zip'):
                    continue
                zp = os.path.join(src_plugins, fn)
                try:
                    with zipfile.ZipFile(zp) as z:
                        meta = {}
                        if 'meta.json' in z.namelist():
                            meta = json.loads(z.read('meta.json').decode('utf-8'))
                        # 解压到一个以 zip 名命名的文件夹
                        dest = os.path.join(plugins_dir, os.path.splitext(fn)[0])
                        os.makedirs(dest, exist_ok=True)
                        z.extractall(dest)
                        # 图片可能在 frames/ 子目录，也可能在顶层
                        frames_dir = dest
                        sub = os.path.join(dest, 'frames')
                        if os.path.isdir(sub):
                            frames_dir = sub
                        meta['_src_dir'] = frames_dir
                        meta['_zip_name'] = fn
                        plug_infos.append(meta)
                except Exception as e:
                    plug_infos.append({'_error': str(e), '_zip_name': fn})

        manifest = {
            'ok': True,
            'exe': exe,
            'config': cfg,
            'assets_dir': assets,
            'frame_count': len(frame_keys),
            'mask_path': mask_path,
            'plugins_dir': plugins_dir,
            'plugins': plug_infos,
        }
        print(json.dumps(manifest, ensure_ascii=False))
    except Exception:
        print(json.dumps({'ok': False, 'error': traceback.format_exc()}, ensure_ascii=False))


if __name__ == '__main__':
    main()
