import unittest

from helpers import Sandbox

from fmm import shellfiles as sf

FAKE = 'cfut_FAKE_VALUE_123'

ZSHRC = '''if [[ -o interactive ]]; then
  plugins=(git docker)
  #autoload -Uz compinit
  source $ZSH/oh-my-zsh.sh
fi

export PATH=$HOME%(home)s/flutter/bin:$PATH
export PATH="/opt/homebrew/opt/openjdk@21/bin:$PATH"
export CPPFLAGS="-I/opt/homebrew/opt/openjdk@21/include"
export JAVA_HOME="/opt/homebrew/opt/openjdk@21"

# >>> conda initialize >>>
__conda_setup="$('/x/miniforge3/bin/conda' 'shell.zsh' 'hook' 2> /dev/null)"
if [ $? -eq 0 ]; then
    eval "$__conda_setup"
fi
unset __conda_setup
# <<< conda initialize <<<


# >>> mamba initialize >>>
export MAMBA_EXE='/x/miniforge3/bin/mamba';
# <<< mamba initialize <<<

export PATH="$HOME/.local/bin:$PATH"
autoload -Uz compinit
compinit
# The following lines have been added by Docker Desktop to enable Docker CLI completions.
fpath=(/x/.docker/completions $fpath)
autoload -Uz compinit
compinit
# End of Docker CLI completions
eval "$(fnm env --use-on-cd)"

# Cloudflare API token (used by flarectl, wrangler, etc.)
export CF_API_TOKEN=%(fake)s
export CLOUDFLARE_API_TOKEN=%(fake)s
export PATH="/opt/homebrew/bin:$PATH"
eval "$(~/.local/bin/mise activate)"

# Added by Antigravity CLI installer
export PATH="%(home)s/.local/bin:$PATH"
'''

ZPROFILE = '''# Setting PATH for Python 3.10
# The original version is saved in .zprofile.pysave
PATH="/Library/Frameworks/Python.framework/Versions/3.10/bin:${PATH}"
export PATH
eval "$(/opt/homebrew/bin/brew shellenv)"

##
# Your previous /x/.zprofile file was backed up as /x/.zprofile.macports-saved_2022-10-04_at_01:28:32
##

# MacPorts Installer addition on 2022-10-04_at_01:28:32: adding an appropriate PATH variable for use with MacPorts.
export PATH="/opt/local/bin:/opt/local/sbin:$PATH"
# Finished adapting your PATH environment variable for use with MacPorts.

# Added by OrbStack: command-line tools and integration
source ~/.orbstack/shell/init.zsh 2>/dev/null || :
'''


class ShellRuleTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.ctx = self.box.ctx
        self.zshrc = self.box.make('.zshrc', ZSHRC % {'fake': FAKE, 'home': self.box.home})
        self.box.make('.zprofile', ZPROFILE)

    def tearDown(self):
        self.box.close()

    def ids(self, plan, name):
        return [m.rule.id for m in plan.get(name, [])]

    def test_conditions_follow_what_still_exists(self):
        self.box.mkdir('miniforge3')
        self.box.mkdir('opt/homebrew/opt/openjdk@21', base=self.box.root)
        self.box.mkdir('.docker/completions')
        self.box.mkdir('opt/homebrew/Cellar/fnm/1.0', base=self.box.root)
        plan = sf.plan_edits(self.ctx, sf.build_rules(self.ctx))
        ids = self.ids(plan, '.zshrc')
        for kept in ('zshrc-conda', 'zshrc-mamba', 'zshrc-openjdk', 'zshrc-docker-completions', 'zshrc-fnm'):
            self.assertNotIn(kept, ids)
        for always in ('zshrc-flutter-wrong', 'zshrc-compinit', 'zshrc-brew-path', 'zshrc-local-bin-dup'):
            self.assertIn(always, ids)

    def test_everything_gone_removes_all_blocks_and_keeps_personal_lines(self):
        plan = sf.plan_edits(self.ctx, sf.build_rules(self.ctx))
        ids = self.ids(plan, '.zshrc')
        self.assertEqual(ids.count('zshrc-compinit'), 1, 'the pair inside the Docker block does not count')
        new = sf.apply(self.zshrc.read_text(), plan['.zshrc'])
        for gone in ('conda initialize', 'mamba initialize', 'openjdk@21', 'fnm env', 'Docker CLI completions',
                     'export PATH="/opt/homebrew/bin:$PATH"', 'Antigravity'):
            self.assertNotIn(gone, new)
        for kept in ('source $ZSH/oh-my-zsh.sh', '#autoload -Uz compinit', 'mise activate',
                     'export PATH="$HOME/.local/bin:$PATH"', 'CF_API_TOKEN'):
            self.assertIn(kept, new)
        self.assertNotIn('\n\n\n', new)
        self.assertIsNone(sf.zsh_check(new))
        profile = sf.apply((self.box.home / '.zprofile').read_text(), plan['.zprofile'])
        self.assertEqual(profile.strip(), 'eval "$(/opt/homebrew/bin/brew shellenv)"')

    def test_diff_never_shows_token(self):
        text = self.zshrc.read_text()
        new = sf.replace_lines(text, sf.keychain_lines(sf.literal_tokens(text)))
        diff = '\n'.join(sf.masked_diff(text, new, '.zshrc'))
        self.assertNotIn(FAKE, diff)
        self.assertIn('security find-generic-password', diff)

    def test_invalid_syntax_is_reported(self):
        self.assertIsNotNone(sf.zsh_check('if [[ -n x ]]; then\n'))

    def test_token_migration_lines(self):
        text = self.zshrc.read_text()
        tokens = sf.literal_tokens(text)
        self.assertEqual(sorted(tokens), ['CF_API_TOKEN', 'CLOUDFLARE_API_TOKEN'])
        self.assertEqual(sf.secrets_to_store(tokens), {sf.KEYCHAIN_SERVICE: FAKE})
        new = sf.replace_lines(text, sf.keychain_lines(tokens))
        self.assertNotIn(FAKE, new)
        self.assertIn('export CLOUDFLARE_API_TOKEN="$CF_API_TOKEN"', new)
        self.assertEqual(sf.literal_tokens(new), {})


if __name__ == '__main__':
    unittest.main()
