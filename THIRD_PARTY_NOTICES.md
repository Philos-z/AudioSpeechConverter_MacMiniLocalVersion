# Third-party source dependencies

This source snapshot references the following upstream repositories as pinned Git submodules. Their original license and notice files remain in each submodule.

- IndexTTS: https://github.com/index-tts/index-tts — commit `d9e41aac89fd00b3d71497fddb287b7f24613712`.
- MLX IndexTTS inference: https://github.com/groxaxo/mlx-indextts2-inference — commit `a6d8fef49b3c36fa8bccaf5a780091e9b864f8e7`.

IndexTTS model resources are identified by `reports/models.lock.json`, including IndexTeam/IndexTTS-2.5 revision `c39ce5ba981572cb187443877ff559dfb246ce63`. Model weights and media examples are not distributed by this repository. FFmpeg, yt-dlp, Whisper, Demucs and Python packages are installed separately under their respective upstream licenses.

This file records dependency provenance. It does not replace or relicense upstream code or model terms.


## Playfair Display 字体

Philos.club水印使用Playfair Display的700字重与紧字距，依原标志字形比较选择。上游为Google Fonts官方仓库： https://github.com/google/fonts/tree/main/ofl/playfairdisplay 。未修改字体文件；完整SIL Open Font License随附在 `assets/fonts/OFL.txt`。字体保存在 `assets/fonts/PlayfairDisplay-Variable.ttf`，用于重建水印，不安装到系统字体库。
