# Running the Exposure Calculator Comparison Notebook

## Quick Start

### Option 1: Run from the project root directory (Recommended)

```bash
cd /raid_ut2/home/jerome/github/research/project/muscat-db
jupyter notebook notebooks/exposure_calculator_comparison.ipynb
```

### Option 2: Run from the notebooks directory

```bash
cd /raid_ut2/home/jerome/github/research/project/muscat-db/notebooks
jupyter notebook exposure_calculator_comparison.ipynb
```

### Option 3: Use JupyterLab (if available)

```bash
cd /raid_ut2/home/jerome/github/research/project/muscat-db
jupyter lab notebooks/exposure_calculator_comparison.ipynb
```

## Why This Matters

The notebook automatically detects your project root by looking for the `src/` directory. This ensures correct imports of:
- `muscat_db.exposure` (from `src/`)
- `exp_time_calculator_legacy` (from `scripts/`)

## If You Still Get Import Errors

### Solution 1: Install in development mode

```bash
cd /raid_ut2/home/jerome/github/research/project/muscat-db
pip install -e .
```

This makes `muscat_db` importable from anywhere.

### Solution 2: Set PYTHONPATH manually

```bash
export PYTHONPATH=/raid_ut2/home/jerome/github/research/project/muscat-db/src:$PYTHONPATH
jupyter notebook notebooks/exposure_calculator_comparison.ipynb
```

### Solution 3: Run Jupyter from the project root

```bash
cd /raid_ut2/home/jerome/github/research/project/muscat-db
jupyter notebook
# Then navigate to notebooks/exposure_calculator_comparison.ipynb in the browser
```

## What the Notebook Does

Once loaded, the notebook will:

1. ✓ Detect the project root automatically
2. ✓ Add `src/` and `scripts/` to Python path
3. ✓ Import both calculators successfully
4. ✓ Run all comparisons
5. ✓ Generate plots and tables

## First Cell Output

If successful, the first cell should output:

```
✓ Imports successful
  - Project root: /raid_ut2/home/jerome/github/research/project/muscat-db
  - Legacy calculator: exp_time_calculator_legacy.exposure_time_calculator
  - Modern calculator: muscat_db.exposure.calc_exptime
```

## Troubleshooting

| Error | Cause | Solution |
|-------|-------|----------|
| `ModuleNotFoundError: No module named 'muscat_db'` | src/ not in path | Run from project root or use `pip install -e .` |
| `ModuleNotFoundError: No module named 'exp_time_calculator_legacy'` | scripts/ not in path | Same as above |
| `FileNotFoundError: [Errno 2] No such file or directory: 'src'` | Wrong working directory | Run from project root |

## Dependencies

Make sure these are installed:

```bash
pip install numpy pandas matplotlib seaborn
```

The notebook uses:
- `numpy` - numerical computing
- `pandas` - data frames
- `matplotlib` - plotting
- `seaborn` - statistical visualization
- `muscat_db` - local package
- `exp_time_calculator_legacy` - local script

## Running Cells

After setup, you can:

1. **Run all cells**: Shift+Ctrl+Enter (or Shift+Cmd+Enter on Mac)
2. **Run current cell**: Ctrl+Enter (or Cmd+Enter on Mac)
3. **Run and advance**: Shift+Enter
4. **Restart kernel**: Kernel → Restart

## Expected Runtime

- Full notebook: ~30-60 seconds
- With plots: ~1-2 minutes

## Saving Results

To save the notebook with output:

1. Run all cells
2. File → Download as → Notebook (.ipynb)
3. Or save directly: Ctrl+S

## Interactive Features

Once running, you can:

✓ Modify target magnitudes in Part 2
✓ Change airmass values in Part 5
✓ Adjust plot parameters
✓ Create your own comparisons
✓ Export results

## Questions?

If you encounter issues:

1. Check the first cell output for the detected project root
2. Verify you're running from the correct directory
3. Try `pip install -e .` from the project root
4. Check that `notebooks/`, `src/`, and `scripts/` exist in the project root

Enjoy the notebook! 🚀
