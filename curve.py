#!/usr/bin/env python3

import copy
import math
import argparse


def interpolate(n, **p):
    """Return n points from the start to end of each provided range.

    >>> list(interpolate(2, x=(1,3), y=(4,8)))
    [{'x': 2.0, 'y': 6.0}, {'x': 3.0, 'y': 8.0}]

    """
    s = {i:(j[1] - j[0]) / n for i, j in p.items()}
    for i in range(1, n+1):
        yield {
            k: p[k][0] + (i * s[k])
            for k in p
        }

class GCode:
    @classmethod
    def parse(cls, line):
        """Parse a line of GCode.

        >>> GCode.parse('G20')
        <GCode: G20 {} >
        >>> GCode.parse('G92 E0 ; Reset extruder')
        <GCode: G92 {'E': '0'} ; Reset extruder>

        """
        line = line.rstrip()
        if ';' in line:
            command, comment = line.split(';', 1)
            comment = ';' + comment
        else:
            command = line
            comment = ''
            
        command = command.strip().split()
        if len(command) > 1:
            args = {
                i[0]: i[1:] for i in command[1:]
            }
        else:
            args = {}
        if command:
            command = command[0]
        else:
            command = ''
        
        return cls(command, args, comment)
    
    def __init__(self, cmd, args, comment):
        self.command = cmd
        self.args = args or {}
        self.comment = comment

    def __repr__(self):
        return f'<GCode: {self.command} {self.args} {self.comment}>'
        
    def __str__(self):
        """Convert back to text.

        >>> str(GCode('G1', {'F': '1800', 'X': 109.0}, '; feed'))
        'G1 F1800 X109.000 ; feed'
        >>> str(GCode('', {}, ';hello'))
        ';hello'

        """
        s = ''
        if self.command:
            s = self.command
        for k in self.args:
            if k == 'E':
                v = f'{float(self.args[k]):.5f}'
            elif k in ('X', 'Y', 'Z'):
                v = f'{float(self.args[k]):.3f}'
            else:
                v = self.args[k]
            s += f' {k}{v}'
        if self.comment:
            if s.strip():
                s = s.strip() + ' '
            if not self.comment.startswith(';'):
                s += ';'
            s += self.comment
        return s

    
class GCodeProcessor:
    def __init__(self):
        self.state = None

    def handle_line(self, line):
        parsed = GCode.parse(line)
        next_state = self.state(parsed)
        while next_state:
            self.state = next_state
            next_state = self.state(parsed)

            
class GCodeScanner(GCodeProcessor):
    def __init__(self, td):
        super().__init__()
        self.state = self.Intro
        self.max_x = 0
        self.min_x = 9e999
        self.max_y = 0
        self.min_y = 9e999
        self.max_z = 0
        self.min_z = 0
        self.target_depression = td

    def xmid(self, x):
        """Transform X to the range (-1,1).

        >>> g = GCodeScanner(1)
        >>> g.max_x = 12.0
        >>> g.min_x = 4.0
        >>> g.xmid(4.0)
        -1.0
        >>> g.xmid(6.0)
        -0.5
        >>> g.xmid(8.0)
        0.0
        >>> g.xmid(12.0)
        1.0
        
        """
        m = self.min_x + ((self.max_x - self.min_x) / 2.0)
        return (x - m) / (self.max_x - m)
        
    def ymid(self, y):
        m = self.min_y + ((self.max_y - self.min_y) / 2.0)
        return (y - m) / (self.max_y - m)

    def layer_depression(self, z):
        """How much depression for a given layer.

        >>> g = GCodeScanner(1)
        >>> g.max_z = 10.0
        >>> g.layer_depression(0)
        0.0
        >>> g.layer_depression(1.0)
        >>> g.layer_depression(10.0)
        
        """
        return self.target_depression * (z / self.max_z) * 4.0
        
    def Intro(self, g):
        if g.comment == ';TYPE:SKIRT':
            return self.SkipSkirt

    def SkipSkirt(self, g):
        if g.comment.startswith(';TYPE'):
            return self.RegionScan

    def RegionScan(self, g):
        if g.command == 'M107':
            return self.EndStage
        if g.command == 'G91':
            return self.SkipRelativePosition
        if g.command not in ('G0', 'G1'):
            return
        
        if 'X' in g.args:
            x = float(g.args['X'])
            self.max_x = max(self.max_x, x)
            self.min_x = min(self.min_x, x)
        if 'Y' in g.args:
            y = float(g.args['Y'])
            self.max_y = max(self.max_y, y)
            self.min_y = min(self.min_y, y)
        if 'Z' in g.args:
            z = float(g.args['Z'])
            self.max_z = max(self.max_z, z)

    def SkipRelativePosition(self, g):
        if g.command == 'G90':
            return self.RegionScan

    def EndStage(self, g):
        pass

            
class GCodeTranslator(GCodeProcessor):
    def __init__(self, model):
        super().__init__()
        self.model = model
        self.state = self.Intro
        self.output = []
        self.first_z = None
        self.layer_z = 0.0
        self.last_x = 0.0
        self.last_y = 0.0
        self.last_z = 0.0
        self.last_e = 0.0
            
    def target_z(self, x, y, z):
        # transform x and y into -1 to 1 coordinate space
        # FIXME: the first layer is being affected
        d = self.model.layer_depression(z - self.first_z)
        x = self.model.xmid(x)
        y = self.model.ymid(y)
        
        s = math.sqrt((2*d) - (d**2))
        f = math.sqrt(1 - (y*s)**2)

        return z + f - 1
            
    def Intro(self, g):
        if g.comment.startswith(';LAYER:'):
            return self.LayerHeader
        else:
            self.output.append(g)

    def LayerHeader(self, g):
        if g.command == 'G0' and 'Z' in g.args:
            return self.LayerCode
        else:
            self.output.append(g)

    def LayerCode(self, g):
        if g.comment.startswith(';LAYER:'):
            return self.LayerHeader

        if g.command == 'M107':
            return self.EndStage
        
        if g.command not in ('G0', 'G1'):
            self.output.append(g)
            return
        
        if g.command == 'G0' and 'Z' in g.args:
            z_height = g.args['Z']
            self.layer_z = float(z_height)
            if self.first_z is None:
                self.first_z = self.layer_z
            
        target_x = float(g.args['X']) if 'X' in g.args else self.last_x
        target_y = float(g.args['Y']) if 'Y' in g.args else self.last_y

        if g.command == 'G0' and 'Z' in g.args:
            g.args['Z'] = self.target_z(target_x, target_y, self.layer_z)
            self.output.append(g)
            self.last_x = target_x
            self.last_y = target_y
            return

        if target_y == self.last_y:
            new_g = copy.deepcopy(g)
            new_g.args['Z'] = self.target_z(target_x, target_y, self.layer_z)
            self.output.append(new_g)
            
        else:
            ir = {
                'X': (self.last_x, target_x),
                'Y': (self.last_y, target_y),
            }
            if 'E' in g.args:
                ir['E'] = (self.last_e, float(g.args['E']))
        
            for point in interpolate(4, **ir):
                point['Z'] = self.target_z(point['X'], point['Y'], self.layer_z)
                new_g = copy.deepcopy(g)
                new_g.args.update(point)
                self.output.append(new_g)
            
        self.last_x = target_x
        self.last_y = target_y
        if 'E' in new_g.args:
            self.last_e = float(new_g.args['E'])

    def EndStage(self, g):
        self.output.append(g)


def main():
    argparser = argparse.ArgumentParser()
    argparser.add_argument('gcode')
    argparser.add_argument('--depress', type=float, default=0.1)

    args = argparser.parse_args()

    with open(args.gcode) as fp:
        lines = fp.readlines()

    model = GCodeScanner(td=args.depress)
    for line in lines:
        model.handle_line(line)

    print(';', model.__dict__)
        
    proc = GCodeTranslator(model)
    for line in lines:
        proc.handle_line(line)

    for line in proc.output:
        print(line)

if __name__ == '__main__':
    main()
