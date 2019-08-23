from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import torch.jit
from torch._jit_internal import Optional
import torch.nn as nn
import torch.nn.functional as F
from common_utils import TestCase, run_tests
from torch.quantization import QuantStub, DeQuantStub, \
    quantize, default_eval_fn, QConfig

class QuantizerTestCase(TestCase):
    def test_compare_qparam_eager_script_default(self):
        class Observer(torch.nn.Module):
            __annotations__ = {'scale' : Optional[torch.Tensor], 'zero_point': Optional[torch.Tensor]}
            def __init__(self):
                super(Observer, self).__init__()
                self.dtype = torch.quint8
                self.qscheme = torch.per_tensor_affine
                self.scale, self.zero_point = None, None

            def forward(self, x):
                self.scale = torch.tensor([2.0])
                self.zero_point = torch.tensor([3])
                return x

            @torch.jit.export
            def calculate_qparams(self):
                return self.scale, self.zero_point

        class WeightObserver(Observer):
            def __init__(self):
                super(WeightObserver, self).__init__()
                self.dtype = torch.qint8

        class TestM(nn.Module):
            def __init__(self, qconfig):
                super(TestM, self).__init__()
                self.conv = nn.Conv2d(3, 1, 3).float()
                self.conv.weight.data.fill_(1.0)
                self.conv.bias.data.fill_(0.01)
                self.qconfig = qconfig
                self.quant = QuantStub()
                self.dequant = DeQuantStub()

            def forward(self, x):
                return self.dequant(self.conv(self.quant(x)))

        class TestScriptM(torch.jit.ScriptModule):
            def __init__(self, init_weight=None):
                super(TestScriptM, self).__init__()
                self.conv = nn.Conv2d(3, 1, 3).float()
                self.conv.bias.data.fill_(0.01)

            @torch.jit.script_method
            def forward(self, x):
                y = self.conv(x)
                return y

        # Test Data
        data = [(torch.randn(10, 3, 10, 10, dtype=torch.float), 1)]

        # Eager mode
        fake_qconfig = QConfig(activation=Observer, weight=WeightObserver)
        eager_module = TestM(fake_qconfig)
        script_module = TestScriptM()
        script_module.conv.weight = torch.nn.Parameter(eager_module.conv.weight.detach())
        quantized_eager_module = quantize(eager_module, default_eval_fn, data)
        torch._C._jit_set_inline_everything_mode(False)

        def get_forward(m):
            return m._c._get_method('forward')
        # Script mode
        # TODO: test jit.script as well
        torch._C._jit_pass_constant_propagation(get_forward(script_module).graph)

        ScriptedObserver = torch.jit.script(Observer())
        ScriptedWeightObserver = torch.jit.script(WeightObserver())
        print('--------- 1. Prepare Quant --------')
        script_module._c = torch._C._jit_pass_prepare_quant(script_module._c,
                                                            "forward",
                                                            ScriptedObserver._c, ScriptedWeightObserver._c)

        # Run ScriptM Model and Collect statistics
        print('--------- 2. Calibration ----------')
        get_forward(script_module)(data[0][0])

        # Insert quantize and dequantize calls
        print('--------- 3. Convert --------------')
        torch._C._jit_pass_insert_quant_dequant(script_module._c, "forward")
        # Note that observer modules are not removed right now
        # print(script_module._c._get_modules())
        # torch._C._jit_pass_custom_pattern_based_rewrite_graph()
        print('--------- 4. Fusion ---------------')
        torch._C._jit_pass_quant_fusion(script_module._c._get_method('forward').graph)
        get_forward(script_module)(data[0][0])
        print(get_forward(script_module).code)
        eager_result = quantized_eager_module(data[0][0])
        script_result = get_forward(script_module)(data[0][0])
        self.assertEqual(eager_result, script_result)
        # Compare results for eager and graph mode

    def test_module_rewriter(self):
        class M(torch.nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.conv = torch.nn.Conv2d(3, 12, 3)
                self.bn = torch.nn.BatchNorm2d(12)

            def forward(self, x):
                return self.bn(self.conv(x))

        torch._C._jit_set_inline_everything_mode(False)
        m = torch.jit.script(M())
        m._c._dump(omit_method_bodies=False)

if __name__ == '__main__':
    run_tests()
