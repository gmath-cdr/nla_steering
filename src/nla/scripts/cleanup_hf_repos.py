"""Delete only named Hugging Face cache repositories after a completed stage."""
import argparse
from huggingface_hub import scan_cache_dir

p = argparse.ArgumentParser()
p.add_argument("repos", nargs="+")
args = p.parse_args()
wanted = set(args.repos)
cache = scan_cache_dir()
revisions = [rev.commit_hash for repo in cache.repos if repo.repo_id in wanted for rev in repo.revisions]
found = {repo.repo_id for repo in cache.repos if repo.repo_id in wanted}
print({"requested": sorted(wanted), "found": sorted(found), "revisions": revisions})
if revisions:
    strategy = cache.delete_revisions(*revisions)
    print({"expected_freed_size": strategy.expected_freed_size_str})
    strategy.execute()
