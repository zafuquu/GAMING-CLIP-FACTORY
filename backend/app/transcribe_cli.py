"""Subprocess entry point used to apply a hard timeout around model inference."""
import argparse
import json
from .transcription import transcribe_file

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--input",required=True)
    args=parser.parse_args()
    result=transcribe_file(args.input)
    print(json.dumps(result,ensure_ascii=False,separators=(",",":")))

if __name__=="__main__":
    main()
