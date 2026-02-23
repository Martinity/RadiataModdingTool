# RadiataCompressionTool
### Overview
A modding tool for radiata stories that can compress/decompress all slz/sle file. 

Not currently built for speed, built to be close to the original game's compressor.

Achieves compression/decompression through header parsing, meaning files that have embedded or nonchained compressed files will need to be split to their own file before this tool can parse them.

### Usage
Executable runs an AI genereated GUI which can be found in createGUI.py. 

If you don't want GUI you can run RadiataCompressionTool.py through cli (it's faster): 
"python RadiataCompressionTool.py compress -h" or "python3 RadiataCompressionTool.py decompress -h" for more information.

### Specifications
Compressor has four mode. Advanced specs can be found near the top of RadiataCompressionTool.py

-0 Store: Direct copy with slz header information.

-1 LZSS: LZSS with 8bit flags and 1byte literals.

-2 RLE/LZSS: LZSS with 8bit flags and 1byte literals. An RLE trap for runs, supporting long and short runs. Long RLE uses an extra byte to store length of run.

-3 LZSS16: LZSS with 16bit flags and 2byte literals.


Compressor generates a similar offset histogram to the original. In my testing this compressor compresses files slightly more than the original. Uses a greedy search algorithm.

Decompression/Encryption generates bit identical outputs compared to original.

### Notable areas of improvement
Speed of compression, currently $O(n)^2$

I have not researched runtime decompression speeds.

Embedded/Nonchained headers could be parsed for before decompression.

Better file input/output stream.

Chained files forced to 1 mode could change if needed, but all original chained archives have a single mode that can recompress all files to the same sector size.
