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
Compressor has four mode;
Store: Direct copy with slz header information.
LZSS: LZSS with 8bit flags and 1byte literals.
RLE/LZSS: LZSS with 8bit flags and 1byte literals. An RLE trap than can trigger long or short RLE. Long RLE uses an extra byte to store length of run.
LZSS16: LZSS with 16bit flags and 2byte literals. Word-aligned.

Compressor generates a similar offset histogram to the original with only slight differences. In my testing this compressor usually compresses files slightly more than the original.

Decompression/Encryption generates bit identical outputs compared to original.

### Areas of Improvments
Speed of compression. 
I have not researched runtime decompression speeds.
Embedded/Nonchained headers could be parsed for before decompression.
Better file input/output stream.
Chained files forced to 1 mode could change if needed, but all original chained archives have a single mode that can recompress all files to the same sector size.
