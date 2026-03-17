# RadiataModdingTool
## Overview
A modding tool for radiata stories that can:
- Pack/Extract ISO contents
- Compress/Decompress SLZ/SLE files
- Pack/Extract Kods archives
- Disk wide file names
  
## Usage
GUI for user-friendly (ai generated), CLI for speed
#### GUI:
`python gui.py`
or windows executable from release
#### CLI:
`python cli.py -h` for more information

## Compressor
Achieves compression/decompression through header parsing, meaning files that have embedded or nonchained compressed files will need to be split to their own file before this tool can parse them.
#### Specifications:
Compressor has four mode. Advanced specs can be found near the top of Components/Compressor.py
1. Store: Direct copy with slz header information.
2. LZSS: LZSS with 8bit flags and 1byte literals.
3. RLE/LZSS: LZSS with 8bit flags and 1byte literals. An RLE trap for runs, supporting long and short runs. Long RLE uses an extra byte to store length of run.
4. LZSS16: LZSS with 16bit flags and 2byte literals.
Compressor generates a similar offset histogram to the original.
Decompression/Encryption generates bit identical outputs compared to original.

## Kods Archiver
Achieves packing/extracting through parsing the original kods. You will need to keep the original kods untill successful repack.
>Currently no support for 0005.kods, 0184.kods, 0185.kods

## Notable areas of improvement
- Research into runtime decompression.
- File IO for embedded/recursive cases.
- Chained files forced to 1 mode could change if needed, but all original chained archives have a single mode that can recompress all files to the same sector size.

### Thanks to;
- r-casanova R for the many helpful commits
- yu.na. for help with the kods archive format
- CUE for the ISO TOC handling
