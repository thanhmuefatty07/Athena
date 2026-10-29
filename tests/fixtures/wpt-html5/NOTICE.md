# Web Platform Tests HTML parser fixtures

The .dat files in this directory are copied from
web-platform-tests/wpt/html/syntax/parsing/resources/ at commit
8a5805234c70967e448b07eb8ae86155f0bf4299.

Upstream source: https://github.com/web-platform-tests/wpt/tree/8a5805234c70967e448b07eb8ae86155f0bf4299/html/syntax/parsing/resources
Upstream license: https://github.com/web-platform-tests/wpt/blob/8a5805234c70967e448b07eb8ae86155f0bf4299/LICENSE.md

These fixtures test HTML document tree construction and include parse-error
annotations. The upstream browser runner compares tree output but ignores the
error annotations. The Athena integration test uses the 57 whole-document
fixtures listed by its pinned corpus. It does not include noscript01.dat,
which is absent from the upstream html5lib_url.html wrapper. It excludes
fragment cases and scripting-disabled cases because the production parser
entry point does not expose those modes. It also excludes
scripted_adoption01.dat, scripted_ark.dat, scripted_foster01.dat, and
scripted_webkit01.dat because their expected trees depend on JavaScript
execution; Athena's parser does not execute page scripts. The test runs the
remaining cases with Athena's default scripting-enabled document parser.

# The 3-Clause BSD License

Copyright © web-platform-tests contributors

Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.
3. Neither the name of the copyright holder nor the names of its contributors may be used to endorse or promote products derived from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
