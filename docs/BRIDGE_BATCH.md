# Make bridge: batch commit

One scenario run = one commit with many files (4 operations total).
Inputs: lookup_url=/repos/OWNER/REPO/commits/main, method=POST, url=.../git/trees, body={base_tree:8af084d0c77e8bb8ee70aaf445c4f966c4fd72f2, tree:[{path,mode,type,content}]}, url2=.../git/commits, body2={message, tree:__TREE2__, parents:[__PARENT__]}, url3=.../git/refs/heads/main, body3={sha:__COMMIT__}.
Single-file mode (lookup_url, method, url, body with 61eb5579d1c370dc610e142613e3ec31df565f70) still works with 2 operations. Do not put files under .github/workflows (needs the workflow scope).
