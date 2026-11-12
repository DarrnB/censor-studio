# Third-party software in the Windows installer

Censor Studio is MIT-licensed. Its bundled dependencies retain their upstream licenses.
The installer places dependency license texts and notices under `_internal/licenses`.
ONNX Runtime's third-party notices are also under `_internal/onnxruntime`.

The [Microsoft DirectML 1.15.4 redistributable](https://www.nuget.org/packages/Microsoft.AI.DirectML/1.15.4)
is provided under Microsoft's separate software license. Its license and third-party
notices are in `_internal/licenses/DirectML`.

## Qt and Qt for Python

The Windows build dynamically links Qt and uses the PySide6 Essentials and Shiboken6
bindings under the GNU Lesser General Public License v3.0. The LGPL and GPL texts
are included in `_internal/licenses/Qt`. These libraries can be modified, replaced
with interface-compatible builds, and reverse engineered for debugging such modifications.
The installer places the replaceable library and plugin files in `_internal/PySide6`
and `_internal/shiboken6`. Source and build instructions for the bundled 6.12.0 versions are available below.

- [Qt 6.12.0 source and downloads](https://www.qt.io/development/offline-installers)
- [Qt 6.12.0 source tree](https://code.qt.io/cgit/qt/qt5.git/?h=v6.12.0)
- [Qt for Python and Shiboken 6.12.0 source archives](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.12.0-src/)
- [Building Qt for Windows](https://doc.qt.io/qt-6.12/windows-building.html)
- [Building PySide6 and Shiboken from source](https://doc.qt.io/qtforpython-6/building_from_source/index.html)

The Censor Studio source and Windows build instructions are included in the
[v1.1.1 source repository](https://github.com/DarrnB/censor-studio/tree/v1.1.1).

## AI models

The installer and repository contain no model weights. The application downloads models
from their upstream repositories and runs them locally. See the README for model sources
and their publishers' license information.
