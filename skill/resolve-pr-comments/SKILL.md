---
name: resolve-pr-comments
description: Fetch GitHub PR review comments, propose fixes with a plan, and resolve threads after approval
license: MIT
compatibility: opencode
metadata:
  audience: developers
  workflow: github
---

## What I do

- Fetch code review comments from a GitHub pull request
- Check out the PR branch
- Analyze each code review comment and categorize it
- Present a detailed plan of proposed fixes (or disagreements) for approval
- After approval, implement fixes, commit, push, reply to threads, and resolve them
- For non-code comments (general PR comments, questions), propose responses in chat without taking action

## When to use me

Use this skill when:

- You have a PR with code review comments that need to be addressed
- You want to systematically review and resolve all feedback
- You need to reply to reviewers explaining what was fixed (or why you disagree)

## Required information

Provide one of:

- **PR URL**: e.g., `https://github.com/owner/repo/pull/123`
- **PR number + repository**: e.g., `PR #123 in owner/repo`
- **PR number only**: If already in the correct repository context

---

## Workflow

## Workflow

### Phase 1: Discovery (Read-Only)

1. **Get PR metadata**: `gh pr view <PR_NUMBER> --repo <OWNER/REPO> --json headRefName,baseRefName,title,url`
2. **Fetch review comments**: `gh api repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/comments` — returns comments with `id`, `body`, `path`, `line`, `diff_hunk`, `user.login`.
3. **Fetch general PR comments**: `gh api repos/<OWNER>/<REPO>/issues/<PR_NUMBER>/comments`
4. **Checkout the PR branch**: `git fetch origin <branch_name> && git checkout <branch_name>`
5. **Read referenced code**: For each comment, read the file at the specified line.

### Phase 2: Analysis & Planning

Categorize each **code review comment** as: (1) **Actionable fix**, (2) **Suggestion to evaluate**, (3) **Question**, or (4) **Disagreement**. General PR comments are not actioned automatically — propose a response in chat.

Present a structured plan:

```
## PR Review Resolution Plan
**PR:** #<number> - <title> | **Branch:** <branch_name> | **Comments:** <X> code, <Y> general

### Code Review Comments
#### [<file>:<line>] by @<reviewer>
> <quoted comment>
**Category:** Actionable fix | **Action:** <description> | **Files:** <list>

#### [<file>:<line>] by @<reviewer>
> <quoted comment>
**Category:** Disagreement | **Reasoning:** <explanation> | **Response:** <reply>

### General PR Comments
#### by @<reviewer>
> <quoted comment> | **Proposed response:** <suggested reply>

**Summary:** <N> fixes, <N> disagreements, <N> questions, <N> general responses
**Ready to proceed?** Reply "yes" to implement, or provide feedback.
```

### Phase 3: Execution (After Approval)

Only proceed after user confirms the plan.

1. **Implement fixes**: Edit files for each approved actionable fix; track addressed comment IDs.
2. **Build, test, commit**: Follow build → test → commit order. Never commit without passing tests.

   ```bash
   npm run build && npm --silent test && git add <files> \
     && git commit -m "Address PR review feedback\n\n- <summary of fix 1>\n- <summary of fix 2>" \
     && git push origin <branch_name>
   ```

   Capture the commit hash for replies.

3. **Reply to threads**:

   ```bash
   gh api repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/comments \
     -X POST -f body="<action or explanation>" -F in_reply_to=<COMMENT_ID>
   ```

   - Fixes: `Fixed in <hash>. <description>`
   - Disagreements: `Current implementation is correct because <reasoning>.`

4. **Resolve threads**: Fetch thread IDs via GraphQL, then resolve each:

   ```bash
   # Get thread IDs
   gh api graphql -f query='query { repository(owner:"<OWNER>", name:"<REPO>") { pullRequest(number: <N>) { reviewThreads(first:100) { nodes { id isResolved comments(first:1) { nodes { databaseId } } } } } } }'
   # Resolve each addressed thread
   gh api graphql -f query='mutation { resolveReviewThread(input: {threadId: "<THREAD_ID>"}) { thread { isResolved } } }'
   ```

5. **Report completion**: Output a summary with commit hash, fixes applied, disagreements explained, threads resolved, and pending items.

## Guidelines

1. **Never auto-execute** — present the plan and wait for approval
2. **Respect disagreements** — explain why clearly and professionally
3. **Group related fixes** from multiple comments on the same issue
4. **Reference commits** in replies so reviewers can verify
5. **Don't resolve questions** — only threads where action was taken
6. **Verify push access** before making changes
7. **Handle conflicts** — notify user if the branch is behind

## Error handling

- **Comment on outdated code**: Note in the plan; fix may need adjustment
- **Ambiguous requests**: Ask for clarification rather than guessing
- **Permission denied**: Report and suggest checking repository access
- **Branch not found**: Verify the PR is still open

## Scripts

This skill does not ship an orchestrator script. Use `gh` + local edit/build/test steps as documented above.

End.
