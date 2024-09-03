require 'set'

class Constraint
  attr_accessor :table, :column, :type

  def initialize(tbl, col, tp)
    @table = tbl
    @column = col
    @type = tp
  end

  def to_s
    return "{ type = \"#{@type}\", tbl = \"#{@table}\", col = \"#{@column}\""
  end
end

class NonNullConstraint < Constraint
  attr_accessor :cond # bool

  def initialize(table, column, c)
    super(table, column, "non-null")
    self.cond = c
  end

  def eql?(other)
    return false if other.class != self.class
    self.table == other.table &&
    self.column == other.column &&
    self.cond == other.cond
  end

  def hash
    return (self.table + self.column + self.type + self.cond.to_s).hash
  end

  def to_s
    return super + " }"
  end
end

class InclusionConstraint < Constraint
  attr_accessor :values # list of strings

  def initialize(table, column, vals)
    super(table, column, "inclusion")
    @values = vals
  end

  def eql?(other)
    return false if other.class != self.class
    self.table == other.table &&
    self.column == other.column &&
    Set.new(self.values) == Set.new(other.values)
  end

  def hash
    return (self.table + self.column + self.type + self.vals.to_s).hash
  end
end

class UniqueConstraint < Constraint
  attr_accessor :case_sensitive # boolean

  def initialize(table, column, cs)
    super(table, column, "unique")
    @case_sisistive = cs
  end

  def eq?(other)
    return false if other.class != self.class
    self.table == other.table &&
    self.column == other.column &&
    self.case_sensitive == other.case_sensitive
  end

  def hash
    return (self.table + self.column + self.type + self.case_sensitive.to_s).hash
  end
end

# Probably will reduce absense constraint to this too, absence = true => presence = false
class PresenceConstraint < Constraint
  attr_accessor :cond # boolean

  def initialize(table, column, condition)
    super(table, column, "presence")
    @cond = condition
  end

  def eq?(other)
    return false if other.class != self.class
    self.table == other.table &&
    self.column == other.column &&
    self.cond == other.cond
  end

  def hash
    return (self.table + self.column + self.type + self.cond.to_s).hash
  end
end

class LengthConstraint < Constraint
  attr_accessor :min, :max # integers

  def initialize(table, column, minimum, maximum)
    super(table, column, "length")
    @min = minimum
    @max = maximum
  end

  def eq?(other)
    return false if other.class != self.class
    self.table == other.table &&
    self.column == other.column &&
    self.min == other.min &&
    self.max == other.max
  end

  def hash
    return (self.table + self.column + self.type + self.min.to_s + self.max.to_s).hash
  end

  def to_s
    return super + ", min = #{@min}, max = #{@max} }"
  end
end

class FormatConstraint < Constraint
  attr_accessor :format # regex object

  def initialize(table, column, frmt)
    super(table, column, "format")
    @format = fmrt
  end

  def eq?(other)
    return false if other.class != self.class
    self.table == other.table &&
    self.column == other.column &&
    self.format == other.format
  end

  def hash
    return (self.table + self.column + self.type + self.format.to_s).hash
  end
end

class NumericalityConstraint < Constraint
  # TODO need to add more stuff see https://guides.rubyonrails.org/active_record_validations.html#numericality
  attr_accessor :cond, :only_integer # booleans

  def initialize(table, column, condition, oi)
    super(table, column, "numericality")
    self.cond = condition
    self.only_integer = oi
  end

  def eq?(other)
    return false if other.class != self.class
    self.table == other.table &&
    self.column == other.column &&
    self.cond == other.cond &&
    self.only_integer == other.only_integer
  end

  def hash
    return (self.table + self.column  + self.type + self.cond.to_s + self.only_integer.to_s).hash
  end
end

class ForeignKeyConstraint < Constraint
  attr_accessor :fk_table, :fk_column # strings

  def initialize(table, column, fktable, fkcolumn)
    super(table, column, "foreignkey")
    @fk_table = fktable
    @fk_column = fkcolumn
  end

  def eq?(other)
    return false if other.class != self.class
    self.table == other.table &&
    self.column == other.column &&
    self.fk_table == other.fk_table &&
    self.fk_column == other.fk_column
  end

  def hash
    return (self.table + self.column + self.type + self.fk_table + self.fk_column).hash
  end
end
