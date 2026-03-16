import argparse
from pathlib import Path

from Components.Compressor import MODE_DISPLAY, start_compression, start_decompression, COMPRESSION_MODES
from Components.Kods import start_kods_packing, start_kods_unpacking
from Components.Iso import unpack_iso

###------------------------- MAIN -----------------------------###

if __name__ == "__main__":
    # arg parsing setup
    parser = argparse.ArgumentParser(description='Radiata Stories Modding Tool - CLI',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
            Examples:
            compress 0030.bin 0030.slz
            compress --mode 2 0007.bin 0007.slz
            compress --chain --modes "3,1,0" 0001_1.bin 0001_2.bin 0001_3.bin 0001.slz
            decompress 0001.slz 0002.sle ./extracted/
            kods pack ./modified_files/ new.kods original.kods
            kods extract 2233.kods ./output/
            iso extract radi.iso ./output/
            """)
    subparsers = parser.add_subparsers(dest='command', required=True)
    # Compressor 
    comp = subparsers.add_parser('compress', help='Compress file(s) to .slz / .sle')
    comp.add_argument('inputs', nargs='+', metavar='INPUT', help='Input file(s)')
    comp.add_argument('output', metavar='OUTPUT', help='Output directory. Include file name if using --chain')
    comp.add_argument('-m', '--mode', type=int, choices=[0,1,2,3], default=3, help='0=Store, 1=LZSS, 2=LZSS/RLE, 3=LZSS16')
    comp.add_argument('--chain', action='store_true', help='For mulitple input files chained into one output file')
    comp.add_argument('--bank', action='store_true', help='For multiple input files as sector-aligned independent blocks')
    comp.add_argument('--modes', type=str, default=None, help='Comma-separated modes per input (e.g. 2,1,0). Requires --chain or --bank')
    comp.add_argument('--sle', action='store_true', help='Compress and encrypt')

    decomp = subparsers.add_parser('decompress', help='Decompress .slz or .sle files')
    decomp.add_argument('inputs', nargs='+', metavar='INPUT', help='Input .slz or .sle file(s)')
    decomp.add_argument('output', metavar='OUTPUT', help='Output directory')
    # Kods
    kods = subparsers.add_parser('kods', help='Kods archive operations')
    kods_sub = kods.add_subparsers(dest='kods_command', required=True)

    pack = kods_sub.add_parser('pack', help='Pack files into KODS archive')
    pack.add_argument('input', metavar='INPUT', help='Input directory')   
    pack.add_argument('output', metavar='OUTPUT', help='Output directory')
    pack.add_argument('original', metavar='ORIGINAL', help='Directory for original Kods archive')

    extract_kods = kods_sub.add_parser('extract', help='Extract KODS archive')
    extract_kods.add_argument('input', metavar='INPUT', help='Input Kods file')   
    extract_kods.add_argument('output', metavar='OUTPUT', help='Output directory')
    # ISO
    iso = subparsers.add_parser('iso', help='ISO handler')
    iso_sub = iso.add_subparsers(dest='iso_command', required=True)

    extract_iso = iso_sub.add_parser('extract', help='Extract ISO archive')
    extract_iso.add_argument('input', metavar='INPUT', help='ISO to extract')
    extract_iso.add_argument('output', metavar='OUTPUT', help='Output directory')

    # arg parsing execution
    args = parser.parse_args()
    if args.command == 'compress': # Compression
        if args.chain and args.bank:
            parser.error('--chain and --bank are mutually exclusive')
        if not (args.chain or args.bank) and len(args.inputs) != 1:
            parser.error('Compression only supports 1 input file at a time. Use --chain or --bank for multi-file archives.')
        # Handle files
        if args.modes:
            if not (args.chain or args.bank):
                parser.error('--modes requires --chain or --bank')
            modes = [int(m) for m in args.modes.split(',')]
            if len(modes) != len(args.inputs):
                parser.error(f'--modes has {len(modes)} values but {len(args.inputs)} inputs given')
            for m in modes:
                if m not in COMPRESSION_MODES:
                    parser.error(f'Invalid mode {m} in --modes')
        else:
            modes = [args.mode] * len(args.inputs)
        # Handle output path
        if args.chain or args.bank:
            output_paths = [args.output]
        else:
            ext = ".sle" if args.sle else ".slz"
            out = Path(args.output)

            if not out.suffix:
                out = out / (Path(args.inputs[0]).stem + ext)
            output_paths = [str(out)]
        
        print(f'Starting compression mode:{args.mode}')
        start_compression(args.inputs, modes, output_paths, args.chain, args.bank)

    elif args.command == 'decompress': # Decompression
        print('Starting decompression')
        start_decompression(args.inputs, args.output)

    elif args.command == 'kods':
        if args.kods_command == 'pack': # Pack Kods
            print('Starting Kods packing')
            start_kods_packing(Path(args.input), Path(args.output), Path(args.original))

        elif args.kods_command == 'extract': # Extract Kods
            print('Starting Kods extraction')
            stats = start_kods_unpacking(Path(args.input), Path(args.output))

            print(f"\nCompleted — {stats['extracted']} unique files extracted")
            if stats.get("aliases_skipped"):
                print(f"   {stats['aliases_skipped']} aliases skipped (duplicates)")
            if stats.get("tail"):
                print(f"   Tail extracted → {stats['tail']}")
            if stats.get("runtime_warning"):
                print(f"   {stats['runtime_warning']}")

    elif args.command == 'iso': # Extract Iso
        if args.iso_command == 'extract':
            unpack_iso(Path(args.input), Path(args.output))

    print('\nDone')
