#!/usr/bin/env python
import argparse
import os

parser = argparse.ArgumentParser()

parser.add_argument('repo', help='data repository')
parser.add_argument('--tract', type=int)
parser.add_argument('--filt', type=str)
parser.add_argument('--output', '-o', default='QA-notebooks', help='output folder')

args = parser.parse_args()

from explorer.notebook import Coadd_QANotebook, VisitMatch_QANotebook, ColorColor_QANotebook

if not os.path.exists(args.output):
    os.makedirs(args.output)

coadd_nb = Coadd_QANotebook(args.repo, args.tract, args.filt)
coadd_nb.write(os.path.join(args.output, 'coadd_{}_{}.ipynb'.format(args.tract, args.filt)))

matched_nb = VisitMatch_QANotebook(args.repo, args.tract, args.filt)
matched_nb.write(os.path.join(args.output, 'visitMatch_{}_{}.ipynb'.format(args.tract, args.filt)))

color_nb = ColorColor_QANotebook(args.repo, args.tract)
color_nb.write(os.path.join(args.output, 'color_{}.ipynb'.format(args.tract)))
