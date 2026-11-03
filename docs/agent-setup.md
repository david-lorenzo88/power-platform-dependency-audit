# Setup for AI agents

Both audit skills follow these steps before anything else. In this file, `<scripts>` means the scripts folder named in the skill.

If you've already done this in the current session, skip it.

Never ask for a token, password or key, and never write one to a file. If someone pastes one, don't save it, and tell them to replace it.

## 1. Check this computer

Run this from the folder the person is working in:

```
python "<scripts>/check_setup.py"
```

If `python` isn't found, try `python3`, or `py -3` on Windows.

For each `[FIX]` line, give the person the fix it prints. Sign-ins open a browser, so ask them to run `az login` and `pac auth create` themselves and tell you when they're done. Then run the check again. Go on only when it prints `Ready.`

If it says this is the audit tool's own repository, ask where the customer's private project folder is, and work from there.

Show the person the account, tenant and PAC profile it printed, and the project folder. Ask them to confirm these are the right customer's, and that the repository is private. People who work for several customers are often signed in to the wrong tenant.

## 2. Load or collect the answers

### If `audit-config.json` exists

Run the check with it, and show the person the values it prints:

```
python "<scripts>/check_setup.py" --config audit-config.json
```

Ask whether they still apply to this run, and change only what they correct. Then go to step 3.

### If it doesn't exist

Ask these together, so the person can answer them in one go. Use a question tool if you have one.

1. What's the unique name of every solution in this release? A release can be several solutions. If they don't know, run `pac solution list --environment <DEV>` once you have the DEV address, and let them choose by display name.
2. What are the DEV and production addresses? Production can be a fresh copy of it.
3. What's the address of the sandbox for dry runs? It must be a copy of production, and never shared DEV or production. They can leave this for later, but Gate 2 can't pass without it.
4. Does this release include each of these: a Power Pages site, SharePoint sites or lists, Azure Functions, and Dataverse plug-ins?
5. If it includes functions or plug-ins, which Azure DevOps organization and project hold the code? There can be several. If it's another Git host, skip this and ask them to clone the branches.
6. If it includes a Power Pages site, what's its ID? Run `pac pages list --environment <DEV>` to help them find it.

## 3. Save the answers

Write the answers to `audit-config.json` in the project folder, using the format of `examples/audit-config.example.json` in the plugin. Leave out settings with no answer, but always answer every part of `includes`. Then run the check with `--config`, and fix anything it reports.

The file holds no secrets, so the person can commit it to the private repository. It never holds repositories or branches, because the skill asks for those on every run. Never write it into the plugin's folder.

## 4. Use the answers

- Use the saved values wherever the skill needs an environment, a solution or an Azure DevOps project. Don't ask for them again in this run.
- Skip what the release doesn't include, and say what you skipped. Tell the person if `includes` rules out an audit they asked for.
- Each audit's `--check` is what proves the person's access. If it fails, report the error and which access is missing, using the access table in the README.
